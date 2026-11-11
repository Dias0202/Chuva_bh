"""Testes do agente com um modelo roteirizado, sem chamar o Bedrock."""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ValidationError

from agent import app as agent_app
from agent.constants import DISCLAIMER
from agent.evaluation import (
    GoldenCase,
    evaluate,
    load_golden_set,
    looks_like_refusal,
    parse_verdict,
    tools_match,
)
from agent.graph import AgentAnswer, AgentSettings, answer, build_graph, load_system_prompt
from agent.tools import (
    ApiClient,
    ForecastArgs,
    KnowledgeBaseRetriever,
    SearchArgs,
    ToolError,
    build_tools,
    select_horizon,
)

ROOT = Path(__file__).resolve().parents[1]
FORECAST = {
    "point": "A521",
    "issued_at": "2026-11-10T18:00",
    "model_version": "3",
    "forecasts": [
        {"horizon_h": 1, "valid_at": "2026-11-10T19:00", "probability": 0.12},
        {"horizon_h": 3, "valid_at": "2026-11-10T21:00", "probability": 0.4},
        {"horizon_h": 6, "valid_at": "2026-11-11T00:00", "probability": 0.55},
    ],
}


class ScriptedModel(BaseChatModel):
    """Devolve respostas prontas, na ordem, e ignora as ferramentas recebidas."""

    responses: list[AIMessage]

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedModel:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        self.calls.append((path, params))
        return FORECAST if path == "/forecast" else {"points": [{"id": "A521"}]}


def _tool_call(name: str, args: dict[str, Any]) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call-{name}"}])


def test_forecast_args_are_validated() -> None:
    assert ForecastArgs(point_id="A521", hour_local=18).hour_local == 18
    for bad in (
        {"point_id": "a521"},
        {"point_id": "A521", "hour_local": 24},
        {"point_id": "A521", "x": 1},
    ):
        with pytest.raises(ValidationError):
            ForecastArgs.model_validate(bad)
    with pytest.raises(ValidationError):
        SearchArgs(query="x")


def test_select_horizon_converts_to_brasilia_and_filters_the_hour() -> None:
    all_hours = select_horizon(FORECAST, None)
    assert [p["hora_brasilia"] for p in all_hours["previsoes"]] == [
        "10/11 16:00",
        "10/11 18:00",
        "10/11 21:00",
    ]
    assert all_hours["previsoes"][1]["probabilidade_pct"] == 40.0

    at_six = select_horizon(FORECAST, 18)
    assert [p["horizon_h"] for p in at_six["previsoes"]] == [3]
    assert "erro" in select_horizon(FORECAST, 8)


def test_api_client_signs_requests_and_maps_errors() -> None:
    session = boto3.Session(
        aws_access_key_id="AKIDEXAMPLE", aws_secret_access_key="secret", region_name="us-east-2"
    )
    seen: list[Any] = []

    def opener(request: Any, timeout: float) -> Any:
        seen.append(request)
        return io.BytesIO(json.dumps(FORECAST).encode())

    client = ApiClient("https://api.example.com/", session, "us-east-2", opener=opener)

    assert client.get("/forecast", {"point": "A521"})["point"] == "A521"
    assert seen[0].full_url == "https://api.example.com/forecast?point=A521"
    assert seen[0].headers["Authorization"].startswith("AWS4-HMAC-SHA256")

    def not_found(request: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(
            request.full_url, 404, "x", Message(), io.BytesIO(b'{"error": "sem previsao"}')
        )

    assert ApiClient("https://a", session, "us-east-2", opener=not_found).get("/f") == {
        "error": "sem previsao"
    }

    def broken(request: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(request.full_url, 503, "x", Message(), io.BytesIO(b""))

    with pytest.raises(ToolError, match="503"):
        ApiClient("https://a", session, "us-east-2", opener=broken).get("/f")
    with pytest.raises(ValueError, match="https"):
        ApiClient("http://a", session, "us-east-2")


def test_retriever_returns_text_and_source() -> None:
    client = MagicMock()
    client.retrieve.return_value = {
        "retrievalResults": [
            {
                "content": {"text": "CAPE é energia"},
                "location": {"s3Location": {"uri": "s3://b/docs/glossario.md"}},
            }
        ]
    }

    docs = KnowledgeBaseRetriever(client, "kb")("O que é CAPE?")

    assert docs == [{"texto": "CAPE é energia", "fonte": "glossario.md"}]


def test_tools_have_no_write_capability_and_work() -> None:
    api = FakeApi()
    tools = {tool.name: tool for tool in build_tools(api, lambda q: [{"texto": q, "fonte": "f"}])}

    assert set(tools) == {"get_forecast", "list_points", "search_docs"}
    assert tools["get_forecast"].invoke({"point_id": "A521", "hour_local": 18})["previsoes"]
    assert tools["search_docs"].invoke({"query": "CAPE"})["documentos"][0]["fonte"] == "f"
    assert build_tools(api, None)[2].invoke({"query": "CAPE"}) == {
        "error": "base de conhecimento não configurada"
    }


def test_graph_calls_the_tool_and_answers_with_the_disclaimer() -> None:
    model = ScriptedModel(
        responses=[
            _tool_call("get_forecast", {"point_id": "A521", "hour_local": 18}),
            AIMessage(content="A chance de chuva às 18h na Pampulha é de 40%."),
        ]
    )
    graph = build_graph(model, build_tools(FakeApi(), None), "prompt")

    result = answer(graph, "Vai chover na Pampulha às 18h?")

    assert result.tools == ["get_forecast"]
    assert "40%" in result.text
    assert result.text.endswith(DISCLAIMER)
    assert "probabilidade_pct" in result.contexts[0]


def test_graph_reports_invalid_tool_arguments_to_the_model() -> None:
    model = ScriptedModel(
        responses=[
            _tool_call("get_forecast", {"point_id": "pampulha"}),
            AIMessage(content="Não encontrei esse ponto."),
        ]
    )
    graph = build_graph(model, build_tools(FakeApi(), None), "prompt")

    result = answer(graph, "E na pampulha?")

    assert "point_id" in result.contexts[0]


def test_graph_stops_runaway_tool_loops() -> None:
    model = ScriptedModel(responses=[_tool_call("list_points", {}) for _ in range(20)])
    graph = build_graph(model, build_tools(FakeApi(), None), "prompt")

    with pytest.raises(Exception, match=r"[Rr]ecursion"):
        answer(graph, "Quais pontos?")


@pytest.mark.parametrize("question", ["", "   ", None, "x" * 1001])
def test_answer_rejects_invalid_questions(question: Any) -> None:
    graph = build_graph(ScriptedModel(responses=[]), [], "prompt")

    with pytest.raises(ValueError, match="pergunta"):
        answer(graph, question)


def test_app_handle_validates_payload() -> None:
    graph = build_graph(ScriptedModel(responses=[AIMessage(content="Oi")]), [], "prompt")

    assert "error" in agent_app.handle("texto solto", graph)
    assert "error" in agent_app.handle({"prompt": ""}, graph)
    assert agent_app.handle({"prompt": "Oi"}, graph)["text"].startswith("Oi")


def test_system_prompt_comes_from_prompt_management_when_configured() -> None:
    managed = MagicMock()
    managed.get_prompt.return_value = {
        "variants": [{"templateConfiguration": {"text": {"text": "publicado"}}}]
    }
    settings = AgentSettings(
        region="us-east-2", api_url="https://a", prompt_id="p", prompt_version="2"
    )

    assert load_system_prompt(settings, managed) == "publicado"
    managed.get_prompt.assert_called_once_with(promptIdentifier="p", promptVersion="2")
    local = load_system_prompt(AgentSettings(region="us-east-2", api_url="https://a"), None)
    assert "Defesa Civil" in local


def test_settings_from_env() -> None:
    settings = AgentSettings.from_env(
        {"FORECAST_API_URL": "https://a", "KNOWLEDGE_BASE_ID": "", "GUARDRAIL_ID": "g"}
    )

    assert settings.knowledge_base_id is None
    assert settings.guardrail_id == "g"


def test_golden_set_is_valid_and_large_enough() -> None:
    cases = load_golden_set(
        (ROOT / "eval" / "golden_set.jsonl").read_text(encoding="utf-8").splitlines()
    )

    assert 30 <= len(cases) <= 50
    known = {"get_forecast", "list_points", "search_docs"}
    assert all(set(c.expected_tools) <= known for c in cases)
    assert sum(c.should_refuse for c in cases) >= 5
    with pytest.raises(ValueError, match="repetidos"):
        load_golden_set([cases[0].model_dump_json()] * 2)


def test_clean_text_removes_model_markup() -> None:
    from agent.graph import clean_text

    raw = "\n<thinking>vou buscar</thinking>A CAPE é energia. </tool>\n"

    assert clean_text(raw) == "A CAPE é energia."


def test_scoring_helpers() -> None:
    assert tools_match(["get_forecast"], ["list_points", "get_forecast"])
    assert not tools_match(["get_forecast"], ["list_points"])
    assert tools_match([], [])
    assert not tools_match([], ["search_docs"])
    assert looks_like_refusal("Só posso ajudar com chuva e tempo.")
    assert not looks_like_refusal("A chance é de 40%.")
    assert parse_verdict('ok {"score": 0.9, "reason": "fiel"}') is not None
    assert parse_verdict('{"score": 7}') is None
    assert parse_verdict("sem json") is None


def test_evaluate_aggregates_metrics_and_survives_errors() -> None:
    cases = [
        GoldenCase(id="a", question="Chove?", category="forecast", expected_tools=["get_forecast"]),
        GoldenCase(id="b", question="Dólar?", category="off_topic", should_refuse=True),
        GoldenCase(id="c", question="Erro?", category="forecast", expected_tools=["get_forecast"]),
    ]

    def agent(question: str) -> AgentAnswer:
        if question == "Erro?":
            raise RuntimeError("falhou")
        if question == "Dólar?":
            return AgentAnswer(text="Só posso ajudar com chuva.")
        return AgentAnswer(text="40%", tools=["get_forecast"], contexts=["40"])

    report = evaluate(cases, agent, lambda _: '{"score": 1.0}')

    summary = report.summary()
    assert summary["tool_accuracy"] == 0.5
    assert summary["refusal_accuracy"] == 1.0
    assert summary["faithfulness"] == 1.0
    assert summary["errors"] == 1
    assert summary["meets_targets"] is False
