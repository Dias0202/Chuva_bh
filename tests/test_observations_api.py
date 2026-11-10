"""Testes da coleta de observações pela API do INMET e da remoção de endpoints."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import boto3
import pytest
import teardown
from botocore.exceptions import ClientError
from moto import mock_aws

from ingestion import handler as ingest
from ingestion.http import FetchError, get_bytes
from ingestion.inmet_api import InmetApiError, fetch_recent, parse_response
from ingestion.open_meteo import HOURLY_COLUMNS
from ingestion.points import POINTS

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

RECORD = {
    "DT_MEDICAO": "2026-11-10",
    "HR_MEDICAO": "1200",
    "CHUVA": "1.4",
    "PRE_INS": "920.1",
    "RAD_GLO": None,
    "TEM_INS": "24.0",
    "PTO_INS": "17,5",
    "UMD_INS": "65",
    "VEN_DIR": "90",
    "VEN_RAJ": "5.0",
    "VEN_VEL": "2.1",
}


def test_parse_response_maps_fields_and_decimal_commas() -> None:
    rows = parse_response(json.dumps([RECORD]).encode(), "A521")

    assert rows[0]["observed_at"] == datetime(2026, 11, 10, 12)
    assert rows[0]["precipitation_mm"] == 1.4
    assert rows[0]["dew_point_c"] == 17.5
    assert rows[0]["radiation_kj_m2"] is None
    assert parse_response(b"", "A521") == []


@pytest.mark.parametrize(
    "body",
    [
        "CHAVE INVÁLIDA!".encode(),
        b"[nao e json",
        json.dumps([{"CHUVA": "1"}]).encode(),
        json.dumps([{**RECORD, "CHUVA": "muito"}]).encode(),
    ],
)
def test_parse_response_rejects_unexpected_content(body: bytes) -> None:
    with pytest.raises(InmetApiError):
        parse_response(body, "A521")


def test_fetch_recent_builds_the_token_url() -> None:
    urls: list[str] = []

    def fetch(url: str) -> bytes:
        urls.append(url)
        return b"[]"

    fetch_recent("A521", "segredo", fetch, date(2026, 11, 9), date(2026, 11, 10))

    assert urls == [
        "https://apitempo.inmet.gov.br/token/estacao/2026-11-09/2026-11-10/A521/segredo"
    ]


def test_get_bytes_hides_the_url_when_labeled(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import urllib.error
    import urllib.request
    from email.message import Message

    def fail(*_: Any, **__: Any) -> Any:
        raise urllib.error.HTTPError("https://x/segredo", 401, "erro", Message(), io.BytesIO())

    monkeypatch.setattr(urllib.request, "urlopen", fail)

    with pytest.raises(FetchError) as error:
        get_bytes("https://x/segredo", label="API do INMET")
    assert "segredo" not in str(error.value)


@pytest.fixture
def s3() -> Iterator[S3Client]:
    with mock_aws():
        client: S3Client = boto3.client("s3", region_name="us-east-1")
        for bucket in ("raw", "curated"):
            client.create_bucket(Bucket=bucket)
        yield client


def _forecast(_: str) -> bytes:
    hourly: dict[str, Any] = {"time": ["2026-11-10T12:00"]}
    hourly.update({name: [1.0] for name in HOURLY_COLUMNS})
    return json.dumps({"hourly": hourly}).encode()


def _ingest(
    s3: S3Client,
    observation_fetch: Any,
    token: str | None = "t",  # noqa: S107
) -> ingest.RunResult:
    return ingest.run(
        datetime(2026, 11, 10, 12),
        s3=s3,
        cloudwatch=MagicMock(),
        raw_bucket="raw",
        curated_bucket="curated",
        fetch=_forecast,
        points=POINTS[:1],
        inmet_token=token,
        observation_fetch=observation_fetch,
    )


def _keys(s3: S3Client) -> list[str]:
    return [o["Key"] for o in s3.list_objects_v2(Bucket="curated").get("Contents", [])]


def test_run_writes_one_observation_file_per_day(s3: S3Client) -> None:
    yesterday = {**RECORD, "DT_MEDICAO": "2026-11-09"}
    body = json.dumps([yesterday, RECORD]).encode()

    result = _ingest(s3, lambda _: body)
    _ingest(s3, lambda _: body)

    assert result.observation_report is not None
    assert result.observation_report.rows == 2
    observations = [k for k in _keys(s3) if k.startswith("observations/")]
    assert sorted(k.rsplit("/", 1)[1] for k in observations) == [
        "api-20261109.parquet",
        "api-20261110.parquet",
    ]


def test_run_without_token_skips_observations(s3: S3Client) -> None:
    result = _ingest(s3, lambda _: pytest.fail("não deveria chamar a API"), token=None)

    assert result.observation_report is None
    assert not [k for k in _keys(s3) if k.startswith("observations/")]


def test_run_fails_after_processing_when_the_api_rejects_the_token(s3: S3Client) -> None:
    with pytest.raises(ingest.IngestionError):
        _ingest(s3, lambda _: "CHAVE INVÁLIDA!".encode())

    assert [k for k in _keys(s3) if k.startswith("forecasts/")]


def test_load_token_caches_and_handles_missing_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest, "_token_cache", {})
    ssm = MagicMock()
    ssm.get_parameter.side_effect = ClientError(
        {"Error": {"Code": "ParameterNotFound"}}, "GetParameter"
    )

    assert ingest.load_token(ssm, "/x") is None
    assert ingest.load_token(ssm, "/x") is None
    assert ssm.get_parameter.call_count == 1
    assert ingest.load_token(ssm, None) is None

    ssm.get_parameter.side_effect = None
    ssm.get_parameter.return_value = {"Parameter": {"Value": "abc"}}
    assert ingest.load_token(ssm, "/y") == "abc"


def test_sagemaker_resources_are_ordered_for_deletion() -> None:
    arns = [
        "arn:aws:sagemaker:us-east-2:1:model/chuva-bh-rain-v1",
        "arn:aws:sagemaker:us-east-2:1:endpoint/chuva-bh-rain",
        "arn:aws:sagemaker:us-east-2:1:endpoint-config/chuva-bh-rain-v1",
        "arn:aws:s3:::bucket",
        "arn:aws:sagemaker:us-east-2:1:model-package/chuva-bh-rain/1",
    ]

    assert teardown.sagemaker_resources(arns) == [
        ("endpoint", "chuva-bh-rain"),
        ("endpoint-config", "chuva-bh-rain-v1"),
        ("model", "chuva-bh-rain-v1"),
    ]


def test_delete_sagemaker_resources_tolerates_missing_ones() -> None:
    client = MagicMock()
    client.delete_model.side_effect = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "Could not find model"}}, "DeleteModel"
    )

    teardown.delete_sagemaker_resources(
        client, [("endpoint", "e"), ("endpoint-config", "c"), ("model", "m")]
    )

    client.delete_endpoint.assert_called_once_with(EndpointName="e")
    client.delete_endpoint_config.assert_called_once_with(EndpointConfigName="c")

    client.delete_endpoint_config.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "lento"}}, "Delete"
    )
    with pytest.raises(ClientError):
        teardown.delete_sagemaker_resources(client, [("endpoint-config", "c")])
