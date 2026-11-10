"""Testes dos clientes de dados, com respostas sintéticas."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime
from email.message import Message
from typing import Any

import pytest

from ingestion.http import FetchError, get_bytes
from ingestion.inmet import InmetFormatError, iter_stations, parse_station_csv
from ingestion.open_meteo import (
    HISTORICAL_URL,
    HOURLY_COLUMNS,
    SOURCE_FORECAST,
    OpenMeteoFormatError,
    build_url,
    fetch_forecast,
    fetch_historical,
    parse_hourly,
)
from ingestion.points import POINT_IDS, POINTS


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "erro", Message(), io.BytesIO())


def test_get_bytes_retries_transient_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes: list[Any] = [_http_error(503), urllib.error.URLError("rede"), _Response(b"ok")]

    def fake(*_: Any, **__: Any) -> Any:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    delays: list[float] = []

    assert get_bytes("https://x", sleep=delays.append) == b"ok"
    assert delays == [1.0, 2.0]


def test_get_bytes_does_not_retry_client_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake(*_: Any, **__: Any) -> Any:
        nonlocal calls
        calls += 1
        raise _http_error(404)

    monkeypatch.setattr(urllib.request, "urlopen", fake)

    with pytest.raises(FetchError, match="HTTP 404"):
        get_bytes("https://x", sleep=lambda _: None)
    assert calls == 1


def test_get_bytes_stops_after_the_attempt_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(*_: Any, **__: Any) -> Any:
        raise TimeoutError

    monkeypatch.setattr(urllib.request, "urlopen", fake)

    with pytest.raises(FetchError, match="3 tentativas"):
        get_bytes("https://x", attempts=3, sleep=lambda _: None)


@pytest.mark.parametrize(
    ("url", "attempts"), [("http://inseguro", 1), ("file:///etc/passwd", 1), ("https://x", 0)]
)
def test_get_bytes_rejects_invalid_arguments(url: str, attempts: int) -> None:
    with pytest.raises(ValueError, match=r"https|attempts"):
        get_bytes(url, attempts=attempts)


def _payload(times: list[str], **overrides: Any) -> bytes:
    hourly: dict[str, Any] = {"time": times}
    hourly.update({name: [1.5] * len(times) for name in HOURLY_COLUMNS})
    hourly.update(overrides)
    return json.dumps({"hourly": hourly}).encode()


def test_build_url_requests_utc_and_metric_wind() -> None:
    url = build_url(HISTORICAL_URL, POINTS[0], start_date="2022-01-01")

    assert url.startswith(HISTORICAL_URL)
    assert "timezone=UTC" in url
    assert "wind_speed_unit=ms" in url
    assert "start_date=2022-01-01" in url


def test_parse_hourly_drops_hours_before_the_run() -> None:
    body = _payload(["2026-11-10T11:00", "2026-11-10T12:00", "2026-11-10T13:00"])

    rows = parse_hourly(body, "A521", SOURCE_FORECAST, datetime(2026, 11, 10, 12))

    assert [row["valid_at"] for row in rows] == [
        datetime(2026, 11, 10, 12),
        datetime(2026, 11, 10, 13),
    ]
    assert rows[0]["issued_at"] == datetime(2026, 11, 10, 12)
    assert rows[0]["point_id"] == "A521"
    assert rows[0]["precipitation_mm"] == 1.5


def test_parse_hourly_keeps_null_values() -> None:
    body = _payload(["2026-11-10T12:00"], cape=[None])

    assert parse_hourly(body, "A521", SOURCE_FORECAST)[0]["cape_j_kg"] is None


@pytest.mark.parametrize(
    "body",
    [
        b"nao e json",
        b'{"error": true}',
        _payload(["2026-11-10T12:00"], cape=[1.0, 2.0]),
        _payload(["2026-11-10T12:00"], cape=["alto"]),
        _payload(["2026-11-10T12:00"], cape=[True]),
    ],
)
def test_parse_hourly_rejects_malformed_payloads(body: bytes) -> None:
    with pytest.raises(OpenMeteoFormatError):
        parse_hourly(body, "A521", SOURCE_FORECAST)


def test_fetch_forecast_returns_raw_body_and_rows() -> None:
    body = _payload(["2026-11-10T12:00"])

    raw, rows = fetch_forecast(POINTS[0], lambda _: body, datetime(2026, 11, 10, 12))

    assert raw == body
    assert len(rows) == 1


def test_fetch_historical_has_no_issue_time() -> None:
    urls: list[str] = []

    def fetch(url: str) -> bytes:
        urls.append(url)
        return _payload(["2022-01-01T00:00"])

    rows = fetch_historical(POINTS[0], fetch, date(2022, 1, 1), date(2022, 1, 2))

    assert rows[0]["issued_at"] is None
    assert "end_date=2022-01-02" in urls[0]


HEADER = (
    "Data;Hora UTC;PRECIPITAÇÃO TOTAL, HORÁRIO (mm);"
    "PRESSAO ATMOSFERICA AO NIVEL DA ESTACAO, HORARIA (mB);RADIACAO GLOBAL (Kj/m²);"
    "TEMPERATURA DO AR - BULBO SECO, HORARIA (°C);TEMPERATURA DO PONTO DE ORVALHO (°C);"
    "UMIDADE RELATIVA DO AR, HORARIA (%);VENTO, DIREÇÃO HORARIA (gr) (° (gr));"
    "VENTO, RAJADA MAXIMA (m/s);VENTO, VELOCIDADE HORARIA (m/s);"
)
CSV = (
    "REGIAO:;SE\nCODIGO (WMO):;A521\n"
    f"{HEADER}\n"
    "2025/01/01;0000 UTC;1,2;920,5;-9999;22,1;18;80;90;5,1;2,2;\n"
    "2025/01/01;0100 UTC;;920,4;;21,9;18,1;81;;;;\n"
)


def test_parse_station_csv_maps_columns_and_missing_values() -> None:
    rows = parse_station_csv(CSV, "A521")

    assert len(rows) == 2
    assert rows[0]["observed_at"] == datetime(2025, 1, 1, 0)
    assert rows[0]["precipitation_mm"] == 1.2
    assert rows[0]["pressure_hpa"] == 920.5
    assert rows[0]["radiation_kj_m2"] is None
    assert rows[0]["wind_speed_ms"] == 2.2
    assert rows[1]["precipitation_mm"] is None


@pytest.mark.parametrize(
    "text",
    [
        "sem cabecalho",
        "Data;Hora UTC;OUTRA COISA\n",
        f"{HEADER}\n2025-01-01;00:00;1;1;1;1;1;1;1;1;1;\n",
        f"{HEADER}\n2025/01/01;0000 UTC;abc;1;1;1;1;1;1;1;1;\n",
    ],
)
def test_parse_station_csv_rejects_unexpected_formats(text: str) -> None:
    with pytest.raises(InmetFormatError):
        parse_station_csv(text, "A521")


def test_iter_stations_reads_only_requested_stations() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("2025/INMET_SE_MG_A521_PAMPULHA_01-01-2025.CSV", CSV.encode("latin-1"))
        bundle.writestr("2025/INMET_SE_SP_A701_SAO PAULO_01-01-2025.CSV", b"ignorado")
        bundle.writestr("leia-me.txt", b"ignorado")

    stations = list(iter_stations(buffer.getvalue(), POINT_IDS))

    assert [point for point, _ in stations] == ["A521"]
    assert len(stations[0][1]) == 2
