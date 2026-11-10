"""Ferramentas do agente. Todas só leem dados; nenhuma escreve ou executa comandos."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Final, Protocol

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    import boto3
    from types_boto3_bedrock_agent_runtime.client import AgentsforBedrockRuntimeClient

# O Brasil não adota horário de verão desde 2019: Brasília fica fixa em UTC-3.
BRASILIA: Final = timezone(timedelta(hours=-3), "BRT")
MAX_DOCUMENTS: Final = 4
MAX_SNIPPET_CHARS: Final = 1200
TIMEOUT_SECONDS: Final = 10


class ToolError(RuntimeError):
    """Falha ao consultar uma fonte. O agente recebe a mensagem e decide o que dizer."""


class ForecastArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    point_id: str = Field(pattern=r"^[A-Z][0-9]{3}$", description="Código do ponto, como A521")
    hour_local: int | None = Field(
        default=None, ge=0, le=23, description="Hora de Brasília pedida, de 0 a 23"
    )


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=3, max_length=300, description="Pergunta sobre a documentação")


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ForecastApi(Protocol):
    def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]: ...


class ApiClient:
    """Cliente da HTTP API da Fase 3, com requisições assinadas (SigV4)."""

    def __init__(
        self,
        base_url: str,
        session: boto3.Session,
        region: str,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("a URL da API precisa ser https")
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.region = region
        self.opener = opener

    def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{self.base_url}{path}{query}"
        credentials = self.session.get_credentials()
        if credentials is None:
            raise ToolError("sem credenciais AWS para assinar a requisição")
        signed = AWSRequest(method="GET", url=url)
        SigV4Auth(credentials.get_frozen_credentials(), "execute-api", self.region).add_auth(signed)
        request = urllib.request.Request(url, headers=dict(signed.headers.items()))  # noqa: S310
        try:
            with self.opener(request, timeout=TIMEOUT_SECONDS) as response:
                body: dict[str, Any] = json.loads(response.read())
                return body
        except urllib.error.HTTPError as error:
            if error.code in (400, 404):
                detail: dict[str, Any] = json.loads(error.read() or b"{}")
                return detail
            raise ToolError(f"API respondeu {error.code}") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ToolError("API indisponível") from error


def select_horizon(forecast: dict[str, Any], hour_local: int | None) -> dict[str, Any]:
    """Converte a previsão em hora de Brasília e escolhe o horizonte da hora pedida."""
    items = []
    for item in forecast.get("forecasts", []):
        valid_at = datetime.fromisoformat(item["valid_at"]).replace(tzinfo=UTC)
        local = valid_at.astimezone(BRASILIA)
        items.append(
            {
                "horizon_h": item["horizon_h"],
                "hora_brasilia": local.strftime("%d/%m %H:%M"),
                "probabilidade_pct": round(100 * float(item["probability"]), 1),
                "_hour": local.hour,
            }
        )
    if hour_local is not None:
        matches = [i for i in items if i["_hour"] == hour_local]
        if not matches:
            return {
                "point": forecast.get("point"),
                "erro": "horário fora dos horizontes disponíveis (1, 3 e 6 horas à frente)",
                "horizontes": [{k: v for k, v in i.items() if k != "_hour"} for i in items],
            }
        items = matches
    return {
        "point": forecast.get("point"),
        "emitida_em_utc": forecast.get("issued_at"),
        "versao_modelo": forecast.get("model_version"),
        "previsoes": [{k: v for k, v in i.items() if k != "_hour"} for i in items],
    }


class KnowledgeBaseRetriever:
    def __init__(self, client: AgentsforBedrockRuntimeClient, knowledge_base_id: str) -> None:
        self.client = client
        self.knowledge_base_id = knowledge_base_id

    def __call__(self, query: str) -> list[dict[str, str]]:
        response = self.client.retrieve(
            knowledgeBaseId=self.knowledge_base_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": MAX_DOCUMENTS}
            },
        )
        documents = []
        for result in response.get("retrievalResults", []):
            location = result.get("location", {}).get("s3Location", {}).get("uri", "")
            documents.append(
                {
                    "texto": result.get("content", {}).get("text", "")[:MAX_SNIPPET_CHARS],
                    "fonte": location.rsplit("/", 1)[-1] or "desconhecida",
                }
            )
        return documents


def build_tools(
    api: ForecastApi, retriever: Callable[[str], list[dict[str, str]]] | None
) -> list[StructuredTool]:
    def get_forecast(point_id: str, hour_local: int | None = None) -> dict[str, Any]:
        forecast = api.get("/forecast", {"point": point_id})
        if "error" in forecast:
            return forecast
        return select_horizon(forecast, hour_local)

    def list_points() -> dict[str, Any]:
        return api.get("/points")

    def search_docs(query: str) -> dict[str, Any]:
        if retriever is None:
            return {"error": "base de conhecimento não configurada"}
        return {"documentos": retriever(query)}

    return [
        StructuredTool.from_function(
            get_forecast,
            name="get_forecast",
            description="Probabilidade de chuva de 1 a 6 horas à frente para um ponto.",
            args_schema=ForecastArgs,
        ),
        StructuredTool.from_function(
            list_points,
            name="list_points",
            description="Lista os pontos monitorados, com código, nome e coordenadas.",
            args_schema=NoArgs,
        ),
        StructuredTool.from_function(
            search_docs,
            name="search_docs",
            description="Busca no glossário, no guia de leitura e nas limitações do modelo.",
            args_schema=SearchArgs,
        ),
    ]
