"""Testes do modo METAR: leitura das fontes, features e scoring."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
from backfill import backfill_metar

from features.build import FEATURE_SETS, METAR_FEATURES, build_dataset, metar_features
from ingestion.metar import (
    MetarFormatError,
    archive_url,
    ceiling,
    parse_archive,
    parse_live,
    weather_flags,
)
from serving.score import feature_rows

ISSUED = datetime(2025, 1, 10, 12)
ARCHIVE = (
    "station,valid,tmpf,dwpf,alti,drct,sknt,gust,wxcodes,skyc1,skyl1,skyc2,skyl2,skyc3,skyl3\n"
    "SBBH,2025-01-10 00:00,71.60,69.80,29.88,90,7.00,M,-TSRA,FEW,4500,BKN,2000,M,M\n"
    "SBBH,2025-01-10 01:00,68.00,64.40,29.94,M,3.00,15,M,SCT,3000,M,M,M,M\n"
)


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        ("-TSRA", (1.0, 1.0)),
        ("RERA", (1.0, 0.0)),
        ("BR", (0.0, 0.0)),
        ("VCTS", (0.0, 1.0)),
        ("M", (0.0, 0.0)),
        (None, (0.0, 0.0)),
    ],
)
def test_weather_flags(codes: str | None, expected: tuple[float, float]) -> None:
    assert weather_flags(codes) == expected


def test_ceiling_uses_the_lowest_broken_or_overcast_layer() -> None:
    assert ceiling([("FEW", 1000.0), ("BKN", 2500.0), ("OVC", 1800.0)]) == 1800.0
    assert ceiling([("SCT", 1000.0)]) is None


def test_parse_archive_converts_units() -> None:
    rows = parse_archive(ARCHIVE)

    assert rows[0]["observed_at"] == datetime(2025, 1, 10, 0)
    assert rows[0]["temperature_c"] == pytest.approx(22.0)
    assert rows[0]["pressure_hpa"] == pytest.approx(1011.8, abs=0.1)
    assert rows[0]["wind_speed_ms"] == pytest.approx(3.6, abs=0.01)
    assert rows[0]["rain"] == 1.0
    assert rows[0]["ceiling_ft"] == 2000.0
    assert rows[1]["wind_direction_deg"] is None
    assert rows[1]["wind_gust_ms"] == pytest.approx(7.72, abs=0.01)


@pytest.mark.parametrize("text", ["Too many requests from your IP", "a,b\n1,2\n"])
def test_parse_archive_rejects_rate_limit_and_unknown_formats(text: str) -> None:
    with pytest.raises(MetarFormatError):
        parse_archive(text)


def test_parse_live_reads_the_aviationweather_json() -> None:
    body = json.dumps(
        [
            {
                "obsTime": int(datetime(2026, 11, 11, 12, 7, tzinfo=UTC).timestamp()),
                "temp": 19,
                "dewp": 16,
                "altim": 1028,
                "wdir": "VRB",
                "wspd": 9,
                "wxString": "-RA",
                "clouds": [{"cover": "BKN", "base": 1500}, {"cover": "OVC", "base": 10000}],
            }
        ]
    ).encode()

    row = parse_live(body)[0]

    assert row["observed_at"] == datetime(2026, 11, 11, 12)
    assert row["wind_direction_deg"] is None
    assert row["ceiling_ft"] == 1500.0
    assert row["rain"] == 1.0
    with pytest.raises(MetarFormatError):
        parse_live(b'{"error": 1}')


def test_archive_url_requests_a_whole_year_in_utc() -> None:
    url = archive_url(2024)

    assert "station=SBBH" in url
    assert "year1=2024" in url
    assert "year2=2025" in url
    assert "tz=Etc%2FUTC" in url


def _metar(hours: int, start: datetime = ISSUED - timedelta(hours=10)) -> pd.DataFrame:
    times = pd.date_range(start, periods=hours, freq="h")
    rain = np.zeros(hours)
    rain[-2] = 1.0
    return pd.DataFrame(
        {
            "point_id": "A521",
            "observed_at": times,
            "source": "metar_iem",
            "temperature_c": 22.0,
            "dew_point_c": 18.0,
            "pressure_hpa": np.linspace(1015, 1012, hours),
            "wind_direction_deg": 90.0,
            "wind_speed_ms": 2.0,
            "wind_gust_ms": np.nan,
            "ceiling_ft": np.nan,
            "rain": rain,
            "thunder": 0.0,
            "weather": None,
        }
    )


def test_metar_features_extend_the_grid_to_the_scoring_hour() -> None:
    features = metar_features(_metar(5), end=pd.Timestamp(ISSUED)).set_index("issued_at")

    last_report = ISSUED - timedelta(hours=6)
    assert features.loc[pd.Timestamp(last_report), "metar_available"] == 1.0
    assert features.loc[pd.Timestamp(last_report), "metar_ceiling_ft"] == 30000.0
    assert features.loc[pd.Timestamp(ISSUED), "metar_available"] == 0.0
    assert np.isnan(features.loc[pd.Timestamp(ISSUED), "metar_ceiling_ft"])
    assert features.loc[pd.Timestamp(last_report), "metar_relative_humidity_pct"] == pytest.approx(
        78.2, abs=0.5
    )


def _forecast() -> pd.DataFrame:
    times = pd.date_range(ISSUED, periods=10, freq="h")
    return pd.DataFrame(
        {
            "point_id": "A521",
            "valid_at": times,
            "issued_at": ISSUED,
            "source": "open_meteo_forecast",
            "precipitation_mm": 1.0,
            "cape_j_kg": 100.0,
            "cloud_cover_pct": 80.0,
            "relative_humidity_pct": 85.0,
        }
    )


def test_build_dataset_without_observations_uses_the_issued_hours() -> None:
    issued = pd.DataFrame({"point_id": ["A521"], "issued_at": [pd.Timestamp(ISSUED)]})

    rows = build_dataset(None, _forecast(), with_labels=False, metar=_metar(11), issued=issued)

    assert len(rows) == 3
    assert set(METAR_FEATURES) <= set(rows.columns)
    assert rows["metar_rain_hours_3h"].iloc[0] == 1.0
    assert rows["fc_precipitation_mm"].notna().all()
    with pytest.raises(ValueError, match="issued"):
        build_dataset(None, _forecast())


def test_feature_rows_in_metar_mode_needs_the_forecast() -> None:
    rows = feature_rows(pd.DataFrame(), _forecast(), ISSUED, "metar", _metar(11), "A521")

    assert rows is not None
    assert list(rows.columns) == ["horizon_h", *FEATURE_SETS["metar"][1:]]
    assert feature_rows(pd.DataFrame(), pd.DataFrame(), ISSUED, "metar", _metar(11), "A521") is None


def test_backfill_metar_waits_and_retries_on_the_rate_limit() -> None:
    responses: list[Any] = [b"Too many requests from your IP", ARCHIVE.encode(), ARCHIVE.encode()]
    waits: list[float] = []

    lines = backfill_metar(lambda _: responses.pop(0), None, "", 2024, 2025, sleep=waits.append)

    assert [line[2] for line in lines] == [2024, 2025]
    assert waits == [90, 15]


def test_backfill_metar_gives_up_after_repeated_rate_limits() -> None:
    with pytest.raises(MetarFormatError):
        backfill_metar(lambda _: b"Too many requests", None, "", 2024, 2024, sleep=lambda _: None)


def test_score_point_uses_metar_mode_only_for_pampulha(monkeypatch: pytest.MonkeyPatch) -> None:
    from serving import score
    from serving.store import ModelConfig

    config = ModelConfig("chuva-bh-rain", "arn", "3", "metar")
    monkeypatch.setattr(score, "load_forecast", lambda *_: _forecast())
    monkeypatch.setattr(score, "load_metar", lambda *_: _metar(11))
    runtime = MagicMock()
    runtime.invoke_endpoint.return_value = {"Body": io.BytesIO(b"0.1\n0.2\n0.3")}
    dynamodb = MagicMock()

    assert (
        score._score_point("A535", ISSUED, config, MagicMock(), runtime, dynamodb, "b", "t")
        is False
    )
    assert (
        score._score_point("A521", ISSUED, config, MagicMock(), runtime, dynamodb, "b", "t") is True
    )
    assert dynamodb.put_item.call_count == 3
