"""Formato das previsões no DynamoDB e da configuração do modelo ativo no SSM."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Final

TTL_DAYS: Final = 90
NO_MODEL: Final = {"status": "no-model"}


@dataclass(frozen=True)
class ModelConfig:
    endpoint_name: str
    model_package_arn: str
    model_version: str
    feature_set: str

    def to_json(self) -> str:
        return json.dumps(asdict(self))


def parse_model_config(raw: str) -> ModelConfig | None:
    """None enquanto nenhum modelo foi aprovado."""
    data = json.loads(raw)
    if data.get("status") == NO_MODEL["status"]:
        return None
    try:
        return ModelConfig(
            endpoint_name=str(data["endpoint_name"]),
            model_package_arn=str(data["model_package_arn"]),
            model_version=str(data["model_version"]),
            feature_set=str(data["feature_set"]),
        )
    except KeyError as error:
        raise ValueError(f"configuração de modelo incompleta: falta {error}") from error


def sort_key(issued_at: datetime, horizon: int) -> str:
    return f"{issued_at:%Y-%m-%dT%H:%M}#{horizon:02d}"


def prediction_item(
    point_id: str, issued_at: datetime, horizon: int, probability: float, model_version: str
) -> dict[str, Any]:
    if not 0.0 <= probability <= 1.0:
        raise ValueError(f"probabilidade fora de [0, 1]: {probability}")
    valid_at = issued_at + timedelta(hours=horizon)
    expires_at = issued_at + timedelta(days=TTL_DAYS)
    return {
        "point_id": {"S": point_id},
        "issue_horizon": {"S": sort_key(issued_at, horizon)},
        "issued_at": {"S": f"{issued_at:%Y-%m-%dT%H:%M}"},
        "valid_at": {"S": f"{valid_at:%Y-%m-%dT%H:%M}"},
        "horizon_h": {"N": str(horizon)},
        "probability": {"N": f"{probability:.6f}"},
        "model_version": {"S": model_version},
        "expires_at": {"N": str(int(expires_at.timestamp()))},
    }
