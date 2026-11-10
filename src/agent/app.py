"""Ponto de entrada no AgentCore Runtime."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from agent.graph import AgentSettings, answer, build_default_graph

logger = logging.getLogger(__name__)
app = BedrockAgentCoreApp()
_graphs: dict[str, Any] = {}


def _graph() -> Any:
    if "default" not in _graphs:
        _graphs["default"] = build_default_graph(AgentSettings.from_env())
    return _graphs["default"]


def handle(payload: Any, graph: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"error": "payload deve ser um objeto JSON com o campo prompt"}
    try:
        result = answer(graph, payload.get("prompt"))
    except ValueError as error:
        return {"error": str(error)}
    return asdict(result)


@app.entrypoint
def invoke(payload: Any) -> dict[str, Any]:
    return handle(payload, _graph())


if __name__ == "__main__":
    app.run()
