#!/usr/bin/env python
"""Roda o golden set contra o agente e grava o relatório.

Uso:
    run_eval.py                       agente local, com as saídas da stack GenAI
    run_eval.py --no-judge            sem LLM-as-a-judge (só ferramentas e recusas)

Chama Bedrock e a API da Fase 3: tem custo por token. Grava eval/results.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError

ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent.evaluation import evaluate, load_golden_set  # noqa: E402
from agent.graph import AgentSettings, answer, build_default_graph  # noqa: E402

REGION: Final = "us-east-2"
JUDGE_MODEL_ID: Final = "us.amazon.nova-pro-v1:0"
GOLDEN_SET: Final = ROOT / "eval" / "golden_set.jsonl"
RESULTS: Final = ROOT / "eval" / "results.json"


def settings_from_stacks(session: boto3.Session) -> AgentSettings:
    cloudformation = session.client("cloudformation")
    outputs: dict[str, str] = {}
    for stack in ("chuva-bh-serving", "chuva-bh-genai"):
        description = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
        outputs.update({o["OutputKey"]: o["OutputValue"] for o in description.get("Outputs", [])})
    return AgentSettings(
        region=REGION,
        api_url=outputs["ApiUrl"],
        knowledge_base_id=outputs.get("KnowledgeBaseId"),
        guardrail_id=outputs.get("GuardrailId"),
        guardrail_version=outputs.get("GuardrailVersion"),
        prompt_id=outputs.get("PromptId"),
        prompt_version=outputs.get("PromptVersion"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--profile")
    args = parser.parse_args(argv)

    try:
        session = boto3.Session(profile_name=args.profile, region_name=REGION)
        graph = build_default_graph(settings_from_stacks(session))
        runtime = session.client("bedrock-runtime")

        def judge(prompt: str) -> str:
            response = runtime.converse(
                modelId=JUDGE_MODEL_ID,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"temperature": 0, "maxTokens": 200},
            )
            return str(response["output"]["message"]["content"][0]["text"])

        cases = load_golden_set(GOLDEN_SET.read_text(encoding="utf-8").splitlines())
        report = evaluate(cases, lambda q: answer(graph, q), None if args.no_judge else judge)
    except (ClientError, BotoCoreError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1

    summary = report.summary()
    detail = [
        {
            "id": r.case.id,
            "tools": r.tools,
            "tool_correct": r.tool_correct,
            "refusal_correct": r.refusal_correct,
            "faithfulness": r.faithfulness,
            "error": r.error,
        }
        for r in report.results
    ]
    RESULTS.write_text(
        json.dumps({"summary": summary, "cases": detail}, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0 if summary["meets_targets"] else 2


if __name__ == "__main__":
    sys.exit(main())
