"""Testes de qualidade, escrita em Parquet, Lambda e backfill, com S3 simulado."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import backfill
import boto3
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

from ingestion import handler as ingest
from ingestion.columns import FORECASTS, OBSERVATIONS, Row
from ingestion.http import FetchError
from ingestion.open_meteo import HOURLY_COLUMNS
from ingestion.points import POINTS
from ingestion.quality import prepare
from ingestion.storage import object_key, write_rows

if TYPE_CHECKING:
    from types_boto3_cloudwatch.client import CloudWatchClient
    from types_boto3_s3.client import S3Client

RAW = "raw-bucket"
CURATED = "curated-bucket"
ISSUED = datetime(2026, 11, 10, 12)
EVENT_TIME = "2026-11-10T12:05:00Z"


@pytest.fixture
def s3() -> Iterator[S3Client]:
    with mock_aws():
        client: S3Client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=RAW)
        client.create_bucket(Bucket=CURATED)
        yield client


@pytest.fixture
def cloudwatch(s3: S3Client) -> CloudWatchClient:
    return boto3.client("cloudwatch", region_name="us-east-1")


def _keys(client: S3Client, bucket: str) -> list[str]:
    return [item["Key"] for item in client.list_objects_v2(Bucket=bucket).get("Contents", [])]


def _observation(hour: int, **values: Any) -> Row:
    row: Row = {
        "point_id": "A521",
        "observed_at": datetime(2025, 1, 1, hour),
        "source": "inmet_bulk",
        "precipitation_mm": 1.0,
    }
    row.update(values)
    return row


def test_prepare_nullifies_physically_impossible_values() -> None:
    rows, report = prepare(
        [_observation(0, precipitation_mm=-1.0), _observation(1, relative_humidity_pct=120.0)],
        OBSERVATIONS,
    )

    assert rows[0]["precipitation_mm"] is None
    assert rows[1]["relative_humidity_pct"] is None
    assert rows[1]["precipitation_mm"] == 1.0
    assert report.out_of_range == 2


def test_prepare_keeps_last_duplicate_and_counts_it() -> None:
    rows, report = prepare(
        [_observation(0, precipitation_mm=1.0), _observation(0, precipitation_mm=2.0)],
        OBSERVATIONS,
    )

    assert [row["precipitation_mm"] for row in rows] == [2.0]
    assert report.rows == 1
    assert report.duplicate_keys == 1


def test_prepare_counts_temporal_gaps_per_point() -> None:
    other = _observation(5)
    other["point_id"] = "A535"

    _, report = prepare([_observation(0), _observation(3), other], OBSERVATIONS)

    assert report.temporal_gaps == 2


def test_prepare_counts_null_measurements() -> None:
    _, report = prepare([_observation(0)], OBSERVATIONS)

    assert report.null_values == len(
        [
            c
            for c in OBSERVATIONS.columns
            if c.glue_type == "double" and c.name != "precipitation_mm"
        ]
    )


def test_prepare_rejects_rows_without_time() -> None:
    with pytest.raises(ValueError, match="observed_at"):
        prepare([{"point_id": "A521"}], OBSERVATIONS)


def test_object_key_uses_hive_partitions() -> None:
    key = object_key(OBSERVATIONS, "A521", datetime(2025, 3, 1), "history.parquet")

    assert key == "observations/year=2025/month=03/point_id=A521/history.parquet"


def test_write_rows_splits_by_month_and_round_trips(s3: S3Client) -> None:
    rows, _ = prepare(
        [
            _observation(0, observed_at=datetime(2025, 1, 31, 23)),
            _observation(0, observed_at=datetime(2025, 2, 1, 0)),
        ],
        OBSERVATIONS,
    )

    keys = write_rows(s3, CURATED, rows, OBSERVATIONS, "A521", "history.parquet")

    assert len(keys) == 2
    body = s3.get_object(Bucket=CURATED, Key=keys[0])["Body"].read()
    table = pq.read_table(io.BytesIO(body))  # type: ignore[no-untyped-call]
    assert table.column_names == [column.name for column in OBSERVATIONS.columns]
    assert "point_id" not in table.column_names
    assert table.num_rows == 1
    assert table["observed_at"][0].as_py() == datetime(2025, 1, 31, 23)


def test_write_rows_rejects_rows_without_time(s3: S3Client) -> None:
    with pytest.raises(ValueError, match="observed_at"):
        write_rows(s3, CURATED, [{"point_id": "A521"}], OBSERVATIONS, "A521", "x.parquet")


def _forecast_body(hours: int = 3) -> bytes:
    times = [(ISSUED + timedelta(hours=i)).isoformat(timespec="minutes") for i in range(hours)]
    hourly: dict[str, Any] = {"time": times}
    hourly.update({name: [10.0] * hours for name in HOURLY_COLUMNS})
    return json.dumps({"hourly": hourly}).encode()


def _run(s3: S3Client, cloudwatch: CloudWatchClient, fetch: Any) -> ingest.RunResult:
    return ingest.run(
        ISSUED,
        s3=s3,
        cloudwatch=cloudwatch,
        raw_bucket=RAW,
        curated_bucket=CURATED,
        fetch=fetch,
    )


def test_run_writes_raw_and_curated_for_every_point(
    s3: S3Client, cloudwatch: CloudWatchClient
) -> None:
    result = _run(s3, cloudwatch, lambda _: _forecast_body())

    assert result.report.rows == 3 * len(POINTS)
    assert result.failed_points == ()
    assert len(_keys(s3, RAW)) == len(POINTS)
    curated = _keys(s3, CURATED)
    assert len(curated) == len(POINTS)
    assert all(key.endswith("issued-20261110T12.parquet") for key in curated)


def test_run_twice_for_the_same_hour_is_idempotent(
    s3: S3Client, cloudwatch: CloudWatchClient
) -> None:
    _run(s3, cloudwatch, lambda _: _forecast_body())
    first = _keys(s3, CURATED), _keys(s3, RAW)

    _run(s3, cloudwatch, lambda _: _forecast_body())

    assert (_keys(s3, CURATED), _keys(s3, RAW)) == first


def test_run_processes_remaining_points_and_raises_on_failure(
    s3: S3Client, cloudwatch: CloudWatchClient
) -> None:
    def fetch(url: str) -> bytes:
        if f"latitude={POINTS[0].latitude}" in url:
            raise FetchError("fora do ar")
        return _forecast_body()

    with pytest.raises(ingest.IngestionError, match=POINTS[0].point_id):
        _run(s3, cloudwatch, fetch)

    assert len(_keys(s3, CURATED)) == len(POINTS) - 1


def test_run_publishes_failures_as_metric(
    s3: S3Client, cloudwatch: CloudWatchClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(cloudwatch, "put_metric_data", lambda **kw: sent.append(kw))

    def fetch(_: str) -> bytes:
        raise FetchError("fora do ar")

    with pytest.raises(ingest.IngestionError):
        _run(s3, cloudwatch, fetch)

    metrics = {m["MetricName"]: m["Value"] for m in sent[0]["MetricData"]}
    assert sent[0]["Namespace"] == ingest.METRIC_NAMESPACE
    assert metrics["PointsFailed"] == len(POINTS)
    assert metrics["RowsWritten"] == 0


def test_parse_issued_at_truncates_to_the_hour_in_utc() -> None:
    assert ingest.parse_issued_at({"scheduled_time": EVENT_TIME}) == ISSUED
    assert ingest.parse_issued_at({"scheduled_time": "2026-11-10T09:05:00-03:00"}) == ISSUED


@pytest.mark.parametrize(
    "event", [{}, {"scheduled_time": 1}, {"scheduled_time": "2026-11-10T12:05:00"}]
)
def test_parse_issued_at_rejects_invalid_events(event: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="scheduled_time"):
        ingest.parse_issued_at(event)


def test_handler_reads_configuration_from_the_environment(
    s3: S3Client, cloudwatch: CloudWatchClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAW_BUCKET", RAW)
    monkeypatch.setenv("CURATED_BUCKET", CURATED)
    metar = json.dumps(
        [{"obsTime": int(ISSUED.replace(tzinfo=UTC).timestamp()), "temp": 22, "dewp": 16}]
    ).encode()

    def fake_get(url: str, **_: Any) -> bytes:
        return metar if "aviationweather" in url else _forecast_body()

    monkeypatch.setattr(ingest, "get_bytes", fake_get)

    result = ingest.handler({"scheduled_time": EVENT_TIME}, None)

    assert result == {"issued_at": ISSUED.isoformat(), "rows": 3 * len(POINTS)}
    metar_keys = [k for k in _keys(s3, CURATED) if k.startswith("metar/")]
    assert metar_keys == ["metar/year=2026/month=11/point_id=A521/live-20261110T12.parquet"]


def test_backfill_forecasts_respects_the_available_range() -> None:
    urls: list[str] = []

    def fetch(url: str) -> bytes:
        urls.append(url)
        hourly: dict[str, Any] = {"time": ["2021-06-01T00:00"]}
        hourly.update({name: [1.0] for name in HOURLY_COLUMNS})
        return json.dumps({"hourly": hourly}).encode()

    lines = backfill.backfill_forecasts(fetch, None, "", 2019, 2021, date(2021, 6, 2))

    assert len(lines) == len(POINTS)
    assert "start_date=2021-03-24" in urls[0]
    assert "end_date=2021-06-01" in urls[0]


def test_render_report_lists_every_line() -> None:
    _, report = prepare([_observation(0)], OBSERVATIONS)

    text = backfill.render_report([("observations", "A521", 2025, report)], date(2026, 11, 10))

    assert "| observations | A521 | 2025 | 1 |" in text
    assert "2026-11-10" in text


def test_main_rejects_inverted_years() -> None:
    with pytest.raises(SystemExit):
        backfill.main(["--start-year", "2026", "--end-year", "2025", "--dry-run"])


def test_main_returns_error_when_a_source_is_down(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*_: Any, **__: Any) -> bytes:
        raise FetchError("fora do ar")

    monkeypatch.setattr(backfill, "get_bytes", fail)

    assert backfill.main(["--dataset", "observations", "--dry-run"]) == 1
    assert "fora do ar" in capsys.readouterr().err


def test_main_writes_history_and_report(
    s3: S3Client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    s3.create_bucket(Bucket="chuva-bh-curated-123456789012-us-east-2")
    report_path = tmp_path / "quality.md"
    monkeypatch.setattr(backfill, "REPORT_PATH", report_path)
    monkeypatch.setattr(backfill, "BULK_URL_TEMPLATE", "https://x/{year}.zip")
    monkeypatch.setattr(backfill, "get_bytes", lambda *_, **__: b"zip")
    monkeypatch.setattr(
        backfill,
        "iter_stations",
        lambda *_: iter([("A521", [_observation(0)])]),
    )

    code = backfill.main(
        ["--dataset", "observations", "--start-year", "2025", "--end-year", "2025"]
    )

    assert code == 0
    assert "A521" in report_path.read_text(encoding="utf-8")
    assert _keys(s3, "chuva-bh-curated-123456789012-us-east-2") == [
        "observations/year=2025/month=01/point_id=A521/history.parquet"
    ]


def test_forecast_dataset_key_includes_the_issue_time() -> None:
    assert FORECASTS.key_columns == ("point_id", "issued_at", "valid_at")
