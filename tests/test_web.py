"""Testes do site: publicação das previsões, chat autenticado e WebStack."""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Iterator
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import boto3
import pytest
import web_ops
from aws_cdk import App, Environment
from aws_cdk.assertions import Match, Template
from botocore.exceptions import ClientError
from moto import mock_aws
from stacks.bundling import SKIP_BUNDLING_CONTEXT
from stacks.common import REGION
from stacks.foundation_stack import FoundationConfig, FoundationStack
from stacks.ml_stack import MlStack
from stacks.serving_stack import ServingStack
from stacks.web_stack import TOKEN_HEADER, WebStack

from serving.store import prediction_item
from web import chat, publisher

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient

TABLE = "forecasts"
ISSUED = datetime(2026, 11, 11, 13)


@pytest.fixture
def aws() -> Iterator[dict[str, Any]]:
    with mock_aws():
        dynamodb: DynamoDBClient = boto3.client("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "point_id", "KeyType": "HASH"},
                {"AttributeName": "issue_horizon", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "point_id", "AttributeType": "S"},
                {"AttributeName": "issue_horizon", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="site")
        yield {"dynamodb": dynamodb, "s3": s3}


def test_snapshot_lists_every_point_and_marks_missing_forecasts(aws: dict[str, Any]) -> None:
    for horizon in (1, 3, 6):
        aws["dynamodb"].put_item(
            TableName=TABLE, Item=prediction_item("A521", ISSUED, horizon, 0.02, "3")
        )

    snapshot = publisher.build_snapshot(aws["dynamodb"], TABLE, datetime(2026, 11, 11, 13, 30))
    publisher.publish(aws["s3"], "site", snapshot)

    by_id = {p["id"]: p for p in snapshot["points"]}
    assert by_id["A521"]["forecast"]["model_version"] == "3"
    assert by_id["A535"]["forecast"] is None
    stored = aws["s3"].get_object(Bucket="site", Key=publisher.SNAPSHOT_KEY)
    assert stored["CacheControl"] == "max-age=300"
    assert json.loads(stored["Body"].read())["generated_at"] == "2026-11-11T13:30:00Z"


def _event(body: Any, sub: str | None = "user-1", encode: bool = False) -> dict[str, Any]:
    raw = body if isinstance(body, str) else json.dumps(body)
    event: dict[str, Any] = {
        "body": base64.b64encode(raw.encode()).decode() if encode else raw,
        "isBase64Encoded": encode,
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": sub} if sub else {}}}},
    }
    return event


def test_parse_question_accepts_plain_and_base64_bodies() -> None:
    assert chat.parse_question(_event({"question": " Chove? "})) == "Chove?"
    assert chat.parse_question(_event({"question": "Chove?"}, encode=True)) == "Chove?"


@pytest.mark.parametrize(
    "body", ["nao e json", {"question": ""}, {"pergunta": "x"}, {"question": "x" * 1001}, ["x"]]
)
def test_parse_question_rejects_invalid_bodies(body: Any) -> None:
    with pytest.raises(chat.BadRequest):
        chat.parse_question(_event(body))


def test_sources_are_extracted_from_search_contexts() -> None:
    contexts = [
        json.dumps({"documentos": [{"fonte": "glossario.md"}, {"fonte": "glossario.md"}]}),
        json.dumps({"previsoes": []}),
        "texto solto",
    ]

    assert chat.sources(contexts) == ["glossario.md"]


def _agent(result: dict[str, Any]) -> MagicMock:
    client = MagicMock()
    client.invoke_agent_runtime.return_value = {"response": io.BytesIO(json.dumps(result).encode())}
    return client


def test_handler_returns_the_agent_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _agent(
        {
            "text": "Baixa chance.",
            "tools": ["get_forecast"],
            "contexts": [json.dumps({"documentos": [{"fonte": "glossario.md"}]})],
        }
    )
    monkeypatch.setattr(chat, "_client", lambda: client)
    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:runtime")

    response = chat.handler(_event({"question": "Chove na Pampulha?"}), None)

    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body == {
        "answer": "Baixa chance.",
        "tools": ["get_forecast"],
        "sources": ["glossario.md"],
    }
    session = client.invoke_agent_runtime.call_args.kwargs["runtimeSessionId"]
    assert session.startswith("web-user-1-")
    assert len(session) >= 33


def test_handler_maps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:runtime")
    failing = MagicMock()
    failing.invoke_agent_runtime.side_effect = ClientError(
        {"Error": {"Code": "Throttling"}}, "InvokeAgentRuntime"
    )
    monkeypatch.setattr(chat, "_client", lambda: failing)

    assert chat.handler(_event({"question": "x"}, sub=None), None)["statusCode"] == 401
    assert chat.handler(_event({"question": ""}), None)["statusCode"] == 400
    assert chat.handler(_event({"question": "Chove?"}), None)["statusCode"] == 502

    monkeypatch.setattr(chat, "_client", lambda: _agent({"error": "pergunta inválida"}))
    assert chat.handler(_event({"question": "Chove?"}), None)["statusCode"] == 400


def test_config_js_exposes_only_public_settings() -> None:
    text = web_ops.config_js("abc123")

    assert text == 'window.CHUVA_CONFIG = {"region": "us-east-2", "clientId": "abc123"};\n'


@pytest.fixture(scope="module")
def template() -> Template:
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
    stack = WebStack(
        app,
        "Web",
        env=env,
        key=foundation.key,
        table=serving.table,
        agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-2:123456789012:runtime/agent",
    )
    return Template.from_stack(stack)


def test_site_bucket_is_private_and_served_through_oac(template: Template) -> None:
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
        },
    )
    template.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)


def test_csp_blocks_third_party_content(template: Template) -> None:
    policy = cast(
        dict[str, Any],
        next(iter(template.find_resources("AWS::CloudFront::ResponseHeadersPolicy").values())),
    )
    csp = policy["Properties"]["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"][
        "ContentSecurityPolicy"
    ]["ContentSecurityPolicy"]

    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp


def test_chat_requires_a_cognito_token_and_is_throttled(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Authorizer",
        {"AuthorizerType": "JWT", "IdentitySource": [f"$request.header.{TOKEN_HEADER}"]},
    )
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Route", {"RouteKey": "POST /api/chat", "AuthorizationType": "JWT"}
    )
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Stage", {"DefaultRouteSettings": {"ThrottlingRateLimit": 1}}
    )


def test_user_pool_has_no_self_sign_up(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Cognito::UserPool",
        {
            "AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True},
            "UserPoolTier": "LITE",
        },
    )


def test_chat_function_can_only_invoke_the_agent(template: Template) -> None:
    statement = next(
        s
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for s in cast(dict[str, Any], policy)["Properties"]["PolicyDocument"]["Statement"]
        if s.get("Sid") == "InvokeAgent"
    )

    assert statement["Action"] == "bedrock-agentcore:InvokeAgentRuntime"
    assert "runtime/agent" in json.dumps(statement["Resource"])


def test_web_resources_have_phase_six_tags(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"Tags": Match.array_with([Match.object_like({"Key": "Phase", "Value": "6"})])},
    )
    template.all_resources_properties("AWS::Logs::LogGroup", {"RetentionInDays": 14})
