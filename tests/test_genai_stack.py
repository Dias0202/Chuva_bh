"""Testes da GenAiStack sobre o template sintetizado."""

from __future__ import annotations

from typing import Any, cast

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Template
from stacks.bundling import SKIP_BUNDLING_CONTEXT
from stacks.common import REGION
from stacks.foundation_stack import FoundationConfig, FoundationStack
from stacks.genai_stack import GenAiStack
from stacks.ml_stack import MlStack
from stacks.serving_stack import ServingStack

from agent.constants import PROMPT_FILE


def _template(chunking: str = "fixed") -> Template:
    app = App(context=SKIP_BUNDLING_CONTEXT)
    env = Environment(account="123456789012", region=REGION)
    foundation = FoundationStack(
        app, "Foundation", env=env, config=FoundationConfig(budget_email="a@example.com")
    )
    ml = MlStack(
        app,
        "Ml",
        env=env,
        curated_bucket=foundation.buckets["curated"],
        artifacts_bucket=foundation.buckets["artifacts"],
        key=foundation.key,
    )
    serving = ServingStack(
        app,
        "Serving",
        env=env,
        curated_bucket=foundation.buckets["curated"],
        sagemaker_role=ml.role,
    )
    stack = GenAiStack(
        app,
        "GenAi",
        env=env,
        kb_bucket=foundation.buckets["kb-docs"],
        key=foundation.key,
        api=serving.api,
        chunking=chunking,
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> Template:
    return _template()


def test_knowledge_base_uses_s3_vectors_and_no_opensearch(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Bedrock::KnowledgeBase", {"StorageConfiguration": {"Type": "S3_VECTORS"}}
    )
    template.has_resource_properties(
        "AWS::S3Vectors::Index", {"Dimension": 1024, "DistanceMetric": "cosine"}
    )
    assert not [t for t in template.to_json()["Resources"].values() if "OpenSearch" in t["Type"]]


@pytest.mark.parametrize(
    ("chunking", "strategy"), [("fixed", "FIXED_SIZE"), ("hierarchical", "HIERARCHICAL")]
)
def test_chunking_strategy_is_selectable(chunking: str, strategy: str) -> None:
    _template(chunking).has_resource_properties(
        "AWS::Bedrock::DataSource",
        {"VectorIngestionConfiguration": {"ChunkingConfiguration": {"ChunkingStrategy": strategy}}},
    )


def test_invalid_chunking_is_rejected() -> None:
    with pytest.raises(ValueError, match="chunking"):
        _template("semantic")


def test_guardrail_denies_off_topic_and_masks_pii(template: Template) -> None:
    guardrail = cast(
        dict[str, Any], next(iter(template.find_resources("AWS::Bedrock::Guardrail").values()))
    )
    props = guardrail["Properties"]

    # Tópicos negados bloqueavam perguntas legítimas em português (ADR-011).
    assert "TopicPolicyConfig" not in props
    pii = {e["Type"] for e in props["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]}
    assert {"EMAIL", "PHONE"} <= pii
    filters = {f["Type"] for f in props["ContentPolicyConfig"]["FiltersConfig"]}
    assert "PROMPT_ATTACK" in filters


def test_prompt_matches_the_versioned_file(template: Template) -> None:
    prompt = cast(
        dict[str, Any], next(iter(template.find_resources("AWS::Bedrock::Prompt").values()))
    )
    text = prompt["Properties"]["Variants"][0]["TemplateConfiguration"]["Text"]["Text"]

    assert text == PROMPT_FILE.read_text(encoding="utf-8")
    template.resource_count_is("AWS::Bedrock::PromptVersion", 1)


def test_runtime_receives_configuration_and_runs_python(template: Template) -> None:
    runtime = cast(
        dict[str, Any],
        next(iter(template.find_resources("AWS::BedrockAgentCore::Runtime").values())),
    )
    props = runtime["Properties"]

    assert props["AgentRuntimeArtifact"]["CodeConfiguration"]["EntryPoint"] == ["agent_main.py"]
    assert set(props["EnvironmentVariables"]) >= {
        "FORECAST_API_URL",
        "GUARDRAIL_ID",
        "KNOWLEDGE_BASE_ID",
    }


def test_log_groups_keep_fourteen_days(template: Template) -> None:
    template.all_resources_properties("AWS::Logs::LogGroup", {"RetentionInDays": 14})


def test_no_wildcard_actions(template: Template) -> None:
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in cast(dict[str, Any], policy)["Properties"]["PolicyDocument"]["Statement"]:
            actions = (
                statement["Action"]
                if isinstance(statement["Action"], list)
                else [statement["Action"]]
            )
            assert not [a for a in actions if a.endswith(":*") or a == "*"]
