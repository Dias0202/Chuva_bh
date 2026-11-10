"""Grafo LangGraph do agente: modelo com ferramentas em laço limitado."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from agent.constants import DEFAULT_MODEL_ID, DISCLAIMER, PROMPT_FILE

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langchain_core.tools import BaseTool
    from langgraph.graph.state import CompiledStateGraph

MAX_QUESTION_CHARS: Final = 1000
# Cada volta modelo -> ferramenta conta dois passos; 10 limita a quatro chamadas de ferramenta.
RECURSION_LIMIT: Final = 10


@dataclass(frozen=True)
class AgentSettings:
    region: str
    api_url: str
    model_id: str = DEFAULT_MODEL_ID
    knowledge_base_id: str | None = None
    guardrail_id: str | None = None
    guardrail_version: str | None = None
    prompt_id: str | None = None
    prompt_version: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> AgentSettings:
        return cls(
            region=environ.get("AWS_REGION", "us-east-2"),
            api_url=environ["FORECAST_API_URL"],
            model_id=environ.get("MODEL_ID", DEFAULT_MODEL_ID),
            knowledge_base_id=environ.get("KNOWLEDGE_BASE_ID") or None,
            guardrail_id=environ.get("GUARDRAIL_ID") or None,
            guardrail_version=environ.get("GUARDRAIL_VERSION") or None,
            prompt_id=environ.get("PROMPT_ID") or None,
            prompt_version=environ.get("PROMPT_VERSION") or None,
        )


@dataclass(frozen=True)
class AgentAnswer:
    text: str
    tools: list[str] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)


def load_system_prompt(settings: AgentSettings, bedrock_agent: Any | None) -> str:
    """Usa a versão publicada no Prompt Management; o arquivo local é a origem dela."""
    if settings.prompt_id and bedrock_agent is not None:
        kwargs = {"promptIdentifier": settings.prompt_id}
        if settings.prompt_version:
            kwargs["promptVersion"] = settings.prompt_version
        prompt = bedrock_agent.get_prompt(**kwargs)
        text: str = prompt["variants"][0]["templateConfiguration"]["text"]["text"]
        return text
    return PROMPT_FILE.read_text(encoding="utf-8")


def build_graph(
    model: BaseChatModel, tools: Sequence[BaseTool], system_prompt: str
) -> CompiledStateGraph[Any, Any, Any, Any]:
    bound = model.bind_tools(list(tools))

    def call_model(state: MessagesState) -> dict[str, list[Any]]:
        messages = [SystemMessage(system_prompt), *state["messages"]]
        return {"messages": [bound.invoke(messages)]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", call_model)
    graph.add_node("tools", ToolNode(list(tools), handle_tool_errors=True))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


def validate_question(question: Any) -> str:
    if not isinstance(question, str) or not question.strip():
        raise ValueError("a pergunta precisa ser um texto não vazio")
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(f"a pergunta passa de {MAX_QUESTION_CHARS} caracteres")
    return question.strip()


def _text(message: AIMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(
        part.get("text", "") if isinstance(part, dict) else str(part) for part in content
    )


def answer(graph: CompiledStateGraph[Any, Any, Any, Any], question: object) -> AgentAnswer:
    result = graph.invoke(
        {"messages": [HumanMessage(validate_question(question))]},
        config={"recursion_limit": RECURSION_LIMIT},
    )
    messages = result["messages"]
    tools = [call["name"] for m in messages if isinstance(m, AIMessage) for call in m.tool_calls]
    contexts = [
        m.content if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False)
        for m in messages
        if isinstance(m, ToolMessage)
    ]
    final = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    text = _text(final) if final is not None else ""
    # O aviso é fixo e não depende do modelo lembrar dele.
    if DISCLAIMER not in text:
        text = f"{text.rstrip()}\n\n{DISCLAIMER}"
    return AgentAnswer(text=text, tools=tools, contexts=contexts)


def default_model(settings: AgentSettings) -> BaseChatModel:
    from langchain_aws import ChatBedrockConverse

    guardrail = None
    if settings.guardrail_id:
        guardrail = {
            "guardrailIdentifier": settings.guardrail_id,
            "guardrailVersion": settings.guardrail_version or "DRAFT",
            "trace": "enabled",
        }
    # O construtor aceita os aliases `model` e `guardrails` desses campos.
    return ChatBedrockConverse(
        model=settings.model_id,
        region_name=settings.region,
        temperature=0,
        max_tokens=600,
        guardrails=guardrail,
    )


def build_default_graph(settings: AgentSettings) -> CompiledStateGraph[Any, Any, Any, Any]:
    import boto3

    from agent.tools import ApiClient, KnowledgeBaseRetriever, build_tools

    session = boto3.Session(region_name=settings.region)
    retriever = (
        KnowledgeBaseRetriever(session.client("bedrock-agent-runtime"), settings.knowledge_base_id)
        if settings.knowledge_base_id
        else None
    )
    tools = build_tools(ApiClient(settings.api_url, session, settings.region), retriever)
    prompt = load_system_prompt(settings, session.client("bedrock-agent"))
    return build_graph(default_model(settings), tools, prompt)
