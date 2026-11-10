#!/usr/bin/env python
"""Operações da Fase 4 que não cabem no CloudFormation.

Uso:
    genai_ops.py sync-kb             envia docs/kb e o model card ao bucket e reindexa
    genai_ops.py enable-logging      liga o invocation logging do Bedrock (14 dias)
    genai_ops.py ask "pergunta"      pergunta ao agente no AgentCore Runtime

Todas as operações usam a conta AWS e têm custo (embeddings, tokens ou execução).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError

ROOT: Final = Path(__file__).resolve().parents[1]
REGION: Final = "us-east-2"
PROJECT: Final = "chuva-bh"
KB_PREFIX: Final = "docs/"
KB_FILES: Final = (*sorted((ROOT / "docs" / "kb").glob("*.md")), ROOT / "docs" / "model-card.md")
INGESTION_TIMEOUT_SECONDS: Final = 600


def outputs(session: boto3.Session, stack: str) -> dict[str, str]:
    description = session.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in description.get("Outputs", [])}


def sync_kb(session: boto3.Session) -> None:
    bucket = outputs(session, f"{PROJECT}-foundation")["KbDocsBucketName"]
    genai = outputs(session, f"{PROJECT}-genai")
    s3 = session.client("s3")
    for path in KB_FILES:
        if path.exists():
            s3.upload_file(str(path), bucket, f"{KB_PREFIX}{path.name}")
            print(f"Enviado {path.name}")

    agent = session.client("bedrock-agent")
    kb_id = genai["KnowledgeBaseId"]
    source = agent.list_data_sources(knowledgeBaseId=kb_id)["dataSourceSummaries"][0]
    job = agent.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=source["dataSourceId"])
    job_id = job["ingestionJob"]["ingestionJobId"]
    deadline = time.monotonic() + INGESTION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status = agent.get_ingestion_job(
            knowledgeBaseId=kb_id, dataSourceId=source["dataSourceId"], ingestionJobId=job_id
        )["ingestionJob"]
        if status["status"] in ("COMPLETE", "FAILED", "STOPPED"):
            print(f"Ingestão {status['status']}: {status.get('statistics', {})}")
            return
        time.sleep(10)
    raise SystemExit("Ingestão não terminou no prazo; confira no console")


def enable_logging(session: boto3.Session) -> None:
    genai = outputs(session, f"{PROJECT}-genai")
    session.client("bedrock").put_model_invocation_logging_configuration(
        loggingConfig={
            "cloudWatchConfig": {
                "logGroupName": genai["InvocationLogGroupName"],
                "roleArn": genai["InvocationLoggingRoleArn"],
            },
            "textDataDeliveryEnabled": True,
            "imageDataDeliveryEnabled": False,
            "embeddingDataDeliveryEnabled": False,
        }
    )
    print("Invocation logging ligado")


def ask(session: boto3.Session, question: str) -> dict[str, Any]:
    arn = outputs(session, f"{PROJECT}-genai")["AgentRuntimeArn"]
    response = session.client("bedrock-agentcore").invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=f"cli-{uuid.uuid4()}",
        payload=json.dumps({"prompt": question}).encode("utf-8"),
    )
    body: dict[str, Any] = json.loads(response["response"].read())
    return body


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync-kb")
    sub.add_parser("enable-logging")
    question = sub.add_parser("ask")
    question.add_argument("question")
    args = parser.parse_args(argv)

    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    try:
        if args.command == "sync-kb":
            sync_kb(session)
        elif args.command == "enable-logging":
            enable_logging(session)
        else:
            print(json.dumps(ask(session, args.question), ensure_ascii=False, indent=2))
    except (ClientError, BotoCoreError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
