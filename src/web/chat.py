"""Lambda do chat do site: recebe a pergunta autenticada e repassa ao agente no AgentCore.

A autenticação é feita antes, pelo autorizador JWT do Cognito na HTTP API. Esta função não
registra o texto das perguntas, que pode conter dados pessoais.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from collections.abc import Mapping
from functools import cache
from typing import Any, Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_QUESTION_CHARS: Final = 1000


class BadRequest(ValueError):
    """Pergunta ausente ou inválida."""


@cache
def _client() -> Any:
    return boto3.client("bedrock-agentcore")


def _response(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def parse_question(event: Mapping[str, Any]) -> str:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise BadRequest("o corpo precisa ser JSON") from error
    question = payload.get("question") if isinstance(payload, dict) else None
    if not isinstance(question, str) or not question.strip():
        raise BadRequest("informe a pergunta no campo question")
    if len(question) > MAX_QUESTION_CHARS:
        raise BadRequest(f"a pergunta passa de {MAX_QUESTION_CHARS} caracteres")
    return question.strip()


def user_id(event: Mapping[str, Any]) -> str:
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        # O autorizador JWT sempre preenche o sub; sem ele a requisição não passou por lá.
        raise PermissionError("requisição sem identidade autenticada")
    return subject


def sources(contexts: list[Any]) -> list[str]:
    """Nomes dos documentos citados pelas buscas na base de conhecimento."""
    found: list[str] = []
    for context in contexts:
        try:
            data = json.loads(context) if isinstance(context, str) else context
        except json.JSONDecodeError:
            continue
        for document in data.get("documentos", []) if isinstance(data, dict) else []:
            name = document.get("fonte")
            if isinstance(name, str) and name not in found:
                found.append(name)
    return found


def ask(client: Any, runtime_arn: str, subject: str, question: str) -> dict[str, Any]:
    response = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=f"web-{subject}-{uuid.uuid4()}",
        payload=json.dumps({"prompt": question}).encode("utf-8"),
    )
    result: dict[str, Any] = json.loads(response["response"].read())
    return result


def handler(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    try:
        subject = user_id(event)
        question = parse_question(event)
    except PermissionError:
        return _response(401, {"error": "não autenticado"})
    except BadRequest as error:
        return _response(400, {"error": str(error)})

    logger.info("Pergunta de %s com %d caracteres", subject, len(question))
    try:
        result = ask(_client(), os.environ["AGENT_RUNTIME_ARN"], subject, question)
    except (ClientError, BotoCoreError, json.JSONDecodeError):
        logger.exception("Falha ao chamar o agente")
        return _response(502, {"error": "o agente não respondeu; tente de novo"})
    if "error" in result:
        return _response(400, {"error": result["error"]})
    return _response(
        200,
        {
            "answer": result.get("text", ""),
            "tools": result.get("tools", []),
            "sources": sources(result.get("contexts", [])),
        },
    )
