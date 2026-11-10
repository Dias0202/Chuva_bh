"""Lambda da HTTP API: GET /points e GET /forecast?point=<id>."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from functools import cache
from typing import TYPE_CHECKING, Any, Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from features.constants import HORIZONS
from ingestion.points import POINT_IDS, POINTS

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DISCLAIMER: Final = (
    "Previsão experimental. Não substitui os alertas oficiais da Defesa Civil e do INMET."
)


class ForecastQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    point: str = Field(pattern=r"^[A-Z][0-9]{3}$")

    @field_validator("point")
    @classmethod
    def known_point(cls, value: str) -> str:
        if value not in POINT_IDS:
            raise ValueError(f"ponto desconhecido; use um de {', '.join(POINT_IDS)}")
        return value


class HorizonForecast(BaseModel):
    horizon_h: int = Field(ge=1, le=24)
    valid_at: str
    probability: float = Field(ge=0.0, le=1.0)


class ForecastResponse(BaseModel):
    point: str
    issued_at: str
    model_version: str
    forecasts: list[HorizonForecast]
    disclaimer: str = DISCLAIMER


@cache
def _dynamodb() -> DynamoDBClient:
    return boto3.client("dynamodb")


def _response(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def latest_forecast(dynamodb: DynamoDBClient, table: str, point: str) -> ForecastResponse | None:
    items = dynamodb.query(
        TableName=table,
        KeyConditionExpression="point_id = :p",
        ExpressionAttributeValues={":p": {"S": point}},
        ScanIndexForward=False,
        Limit=len(HORIZONS),
    )["Items"]
    if not items:
        return None
    issued_at = items[0]["issued_at"]["S"]
    latest = [item for item in items if item["issued_at"]["S"] == issued_at]
    return ForecastResponse(
        point=point,
        issued_at=issued_at,
        model_version=latest[0]["model_version"]["S"],
        forecasts=sorted(
            (
                HorizonForecast(
                    horizon_h=int(item["horizon_h"]["N"]),
                    valid_at=item["valid_at"]["S"],
                    probability=float(item["probability"]["N"]),
                )
                for item in latest
            ),
            key=lambda f: f.horizon_h,
        ),
    )


def route(event: Mapping[str, Any], dynamodb: DynamoDBClient, table: str) -> dict[str, Any]:
    key = event.get("routeKey")
    if key == "GET /points":
        return _response(
            200,
            {
                "points": [
                    {
                        "id": p.point_id,
                        "name": p.name,
                        "latitude": p.latitude,
                        "longitude": p.longitude,
                    }
                    for p in POINTS
                ]
            },
        )
    if key == "GET /forecast":
        try:
            query = ForecastQuery.model_validate(event.get("queryStringParameters") or {})
        except ValidationError as error:
            details = [
                {"field": ".".join(map(str, e["loc"])), "error": e["msg"]} for e in error.errors()
            ]
            return _response(400, {"error": "parâmetros inválidos", "details": details})
        forecast = latest_forecast(dynamodb, table, query.point)
        if forecast is None:
            return _response(404, {"error": "sem previsão para o ponto", "point": query.point})
        return _response(200, forecast.model_dump())
    return _response(404, {"error": "rota não encontrada"})


def handler(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    try:
        return route(event, _dynamodb(), os.environ["TABLE_NAME"])
    except (ClientError, BotoCoreError):
        logger.exception("Falha ao consultar previsões")
        return _response(503, {"error": "serviço indisponível"})
