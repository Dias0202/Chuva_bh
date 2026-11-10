"""Testa o teardown contra serviços simulados pelo moto."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
import teardown
from botocore.exceptions import ClientError, WaiterError
from moto import mock_aws
from teardown import AwsClients, StackResource, TeardownError

STACK = teardown.PHASE_STACKS[0]
PROJECT_TAGS = [
    {"Key": "Project", "Value": "chuva-bh"},
    {"Key": "Phase", "Value": "0"},
    {"Key": "Owner", "Value": "dias"},
]
BUCKET = "chuva-bh-raw-test"
POLICY_NAME = "chuva-bh-kill-switch-deny"
TEMPLATE = json.dumps(
    {
        "Resources": {
            "RawBucket": {
                "Type": "AWS::S3::Bucket",
                "Properties": {"BucketName": BUCKET, "Tags": PROJECT_TAGS},
            },
            "DenyPolicy": {
                "Type": "AWS::IAM::ManagedPolicy",
                "Properties": {
                    "ManagedPolicyName": POLICY_NAME,
                    "PolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {"Effect": "Deny", "Action": "sagemaker:Create*", "Resource": "*"}
                        ],
                    },
                },
            },
        }
    }
)
ASSUME_ROLE_DOCUMENT = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "budgets.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
)


@pytest.fixture
def clients() -> Iterator[AwsClients]:
    with mock_aws():
        yield AwsClients.create()


@pytest.fixture
def deployed(clients: AwsClients) -> AwsClients:
    clients.cloudformation.create_stack(
        StackName=STACK,
        TemplateBody=TEMPLATE,
        Tags=PROJECT_TAGS,  # type: ignore[arg-type]
        Capabilities=["CAPABILITY_NAMED_IAM"],
    )
    return clients


def create_bucket(clients: AwsClients, name: str) -> None:
    clients.s3.create_bucket(
        Bucket=name, CreateBucketConfiguration={"LocationConstraint": "us-east-2"}
    )


def policy_arn(clients: AwsClients) -> str:
    policies = clients.iam.list_policies(Scope="Local")["Policies"]
    return next(p["Arn"] for p in policies if p["PolicyName"] == POLICY_NAME)


def stack_exists(clients: AwsClients, name: str = STACK) -> bool:
    return teardown.stack_tags(clients, name) is not None


def client_error(code: str, message: str = "erro") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Operation")


def mock_clients(**overrides: Any) -> AwsClients:
    defaults: dict[str, Any] = {
        "cloudformation": MagicMock(),
        "s3": MagicMock(),
        "iam": MagicMock(),
        "cloudtrail": MagicMock(),
        "tagging": MagicMock(),
        "sts": MagicMock(),
    }
    return AwsClients(**{**defaults, **overrides})


def test_dry_run_lists_resources_and_changes_nothing(
    deployed: AwsClients, capsys: pytest.CaptureFixture[str]
) -> None:
    deployed.s3.put_object(Bucket=BUCKET, Key="a.json", Body=b"{}")

    teardown.run(deployed, [0], dry_run=True)

    output = capsys.readouterr().out
    assert "AWS::S3::Bucket" in output
    assert BUCKET in output
    assert POLICY_NAME in output
    assert "Dry-run" in output
    assert stack_exists(deployed)
    assert deployed.s3.list_objects_v2(Bucket=BUCKET)["KeyCount"] == 1


def test_teardown_empties_buckets_and_removes_the_stack(deployed: AwsClients) -> None:
    deployed.s3.put_object(Bucket=BUCKET, Key="a.json", Body=b"{}")

    teardown.run(deployed, [0], dry_run=False)

    assert not stack_exists(deployed)
    buckets = [b["Name"] for b in deployed.s3.list_buckets()["Buckets"]]
    assert BUCKET not in buckets


def test_teardown_is_idempotent(deployed: AwsClients, capsys: pytest.CaptureFixture[str]) -> None:
    teardown.run(deployed, [0], dry_run=False)
    capsys.readouterr()

    teardown.run(deployed, [0], dry_run=False)

    assert "Nenhuma stack do projeto" in capsys.readouterr().out


def test_kill_switch_policy_attached_outside_cloudformation_is_detached(
    deployed: AwsClients,
) -> None:
    arn = policy_arn(deployed)
    deployed.iam.create_role(RoleName="alvo", AssumeRolePolicyDocument=ASSUME_ROLE_DOCUMENT)
    deployed.iam.attach_role_policy(RoleName="alvo", PolicyArn=arn)
    deployed.iam.create_user(UserName="pessoa")
    deployed.iam.attach_user_policy(UserName="pessoa", PolicyArn=arn)
    deployed.iam.create_group(GroupName="grupo")
    deployed.iam.attach_group_policy(GroupName="grupo", PolicyArn=arn)

    teardown.detach_policy(deployed, arn)

    entities = deployed.iam.list_entities_for_policy(PolicyArn=arn)
    assert entities["PolicyRoles"] == []
    assert entities["PolicyUsers"] == []
    assert entities["PolicyGroups"] == []


def test_detach_policy_tolerates_a_policy_already_gone(clients: AwsClients) -> None:
    teardown.detach_policy(clients, "arn:aws:iam::123456789012:policy/inexistente")


def test_refuses_stack_without_the_project_tag(clients: AwsClients) -> None:
    clients.cloudformation.create_stack(
        StackName=STACK,
        TemplateBody=TEMPLATE,
        Tags=[{"Key": "Project", "Value": "outro"}],
        Capabilities=["CAPABILITY_NAMED_IAM"],
    )

    with pytest.raises(TeardownError, match="Project=chuva-bh"):
        teardown.run(clients, [0], dry_run=False)

    assert stack_exists(clients)


def test_refuses_foundation_while_a_later_phase_exists(deployed: AwsClients) -> None:
    deployed.cloudformation.create_stack(
        StackName=teardown.PHASE_STACKS[1],
        TemplateBody=json.dumps(
            {"Resources": {"Other": {"Type": "AWS::S3::Bucket", "Properties": {}}}}
        ),
        Tags=PROJECT_TAGS,  # type: ignore[arg-type]
    )

    with pytest.raises(TeardownError, match="chuva-bh-ingestion"):
        teardown.run(deployed, [0], dry_run=False)

    assert stack_exists(deployed)


def test_all_removes_every_phase_including_foundation(deployed: AwsClients) -> None:
    later = teardown.PHASE_STACKS[1]
    deployed.cloudformation.create_stack(
        StackName=later,
        TemplateBody=json.dumps(
            {"Resources": {"Other": {"Type": "AWS::S3::Bucket", "Properties": {}}}}
        ),
        Tags=PROJECT_TAGS,  # type: ignore[arg-type]
    )

    teardown.run(deployed, teardown.select_phases(None, all_phases=True), dry_run=False)

    assert not stack_exists(deployed)
    assert not stack_exists(deployed, later)


def test_all_phases_are_processed_from_last_to_first() -> None:
    assert teardown.select_phases(None, all_phases=True) == [5, 4, 3, 2, 1, 0]


def test_select_phases_requires_a_scope() -> None:
    with pytest.raises(TeardownError, match="--phase"):
        teardown.select_phases(None, all_phases=False)


def test_empty_bucket_removes_every_version_and_delete_marker(clients: AwsClients) -> None:
    create_bucket(clients, "versionado")
    clients.s3.put_bucket_versioning(
        Bucket="versionado", VersioningConfiguration={"Status": "Enabled"}
    )
    clients.s3.put_object(Bucket="versionado", Key="k", Body=b"1")
    clients.s3.put_object(Bucket="versionado", Key="k", Body=b"2")
    clients.s3.delete_object(Bucket="versionado", Key="k")

    assert teardown.empty_bucket(clients, "versionado") == 3

    listing = clients.s3.list_object_versions(Bucket="versionado")
    assert "Versions" not in listing
    assert "DeleteMarkers" not in listing


def test_empty_bucket_tolerates_a_bucket_already_gone(clients: AwsClients) -> None:
    assert teardown.empty_bucket(clients, "nao-existe") == 0


def test_empty_bucket_reports_partial_failures() -> None:
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [
        {"Versions": [{"Key": "k", "VersionId": "1"}]}
    ]
    s3.delete_objects.return_value = {"Errors": [{"Key": "k", "Code": "AccessDenied"}]}

    with pytest.raises(TeardownError, match="AccessDenied"):
        teardown.empty_bucket(mock_clients(s3=s3), "bucket")


def test_stop_trails_stops_logging_and_tolerates_missing_trail(clients: AwsClients) -> None:
    create_bucket(clients, "trail-bucket")
    clients.cloudtrail.create_trail(Name="trilha", S3BucketName="trail-bucket")
    clients.cloudtrail.start_logging(Name="trilha")

    teardown.stop_trails(clients, ["trilha", "inexistente"])

    assert clients.cloudtrail.get_trail_status(Name="trilha")["IsLogging"] is False


def test_stop_trails_propagates_unexpected_errors() -> None:
    cloudtrail = MagicMock()
    cloudtrail.stop_logging.side_effect = client_error("AccessDeniedException")

    with pytest.raises(ClientError):
        teardown.stop_trails(mock_clients(cloudtrail=cloudtrail), ["trilha"])


def _stack_with_one_bucket(cloudformation: MagicMock) -> None:
    cloudformation.get_paginator.return_value.paginate.return_value = [
        {
            "StackResourceSummaries": [
                {
                    "ResourceType": teardown.TYPE_BUCKET,
                    "LogicalResourceId": "Audit",
                    "PhysicalResourceId": "audit-bucket",
                }
            ]
        }
    ]


def test_delete_retries_after_late_delivery_blocks_the_bucket() -> None:
    cloudformation = MagicMock()
    _stack_with_one_bucket(cloudformation)
    failure = WaiterError("stack_delete_complete", "DELETE_FAILED", {})
    cloudformation.get_waiter.return_value.wait.side_effect = [failure, None]
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = []

    teardown.delete_stack(mock_clients(cloudformation=cloudformation, s3=s3), STACK)

    assert cloudformation.delete_stack.call_count == 2
    assert s3.get_paginator.return_value.paginate.call_count == 2


def test_delete_gives_up_after_the_attempt_limit() -> None:
    cloudformation = MagicMock()
    _stack_with_one_bucket(cloudformation)
    failure = WaiterError("stack_delete_complete", "DELETE_FAILED", {})
    cloudformation.get_waiter.return_value.wait.side_effect = failure
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = []

    with pytest.raises(TeardownError, match="3 tentativas"):
        teardown.delete_stack(mock_clients(cloudformation=cloudformation, s3=s3), STACK)

    assert cloudformation.delete_stack.call_count == teardown.MAX_DELETE_ATTEMPTS


def test_stack_tags_propagates_unexpected_errors() -> None:
    cloudformation = MagicMock()
    cloudformation.describe_stacks.side_effect = client_error("AccessDenied")

    with pytest.raises(ClientError):
        teardown.stack_tags(mock_clients(cloudformation=cloudformation), STACK)


def test_tagged_resources_outside_the_stack_are_reported(
    deployed: AwsClients, capsys: pytest.CaptureFixture[str]
) -> None:
    create_bucket(deployed, "chuva-bh-orfao")
    deployed.s3.put_bucket_tagging(
        Bucket="chuva-bh-orfao",
        Tagging={"TagSet": PROJECT_TAGS},  # type: ignore[typeddict-item]
    )

    teardown.run(deployed, [0], dry_run=True)

    output = capsys.readouterr().out
    assert "fora das stacks" in output
    assert "arn:aws:s3:::chuva-bh-orfao" in output


def test_arns_outside_stacks_ignores_arns_owned_by_a_stack() -> None:
    resources = [StackResource("AWS::S3::Bucket", "B", "dentro")]
    arns = ["arn:aws:s3:::dentro", "arn:aws:s3:::fora"]

    assert teardown.arns_outside_stacks(arns, resources) == ["arn:aws:s3:::fora"]


def test_leftovers_are_reported_after_teardown(capsys: pytest.CaptureFixture[str]) -> None:
    arn = "arn:aws:kms:us-east-1:123456789012:key/abc"
    cloudformation = MagicMock()
    cloudformation.describe_stacks.side_effect = client_error(
        "ValidationError", "Stack with id x does not exist"
    )
    tagging = MagicMock()
    tagging.get_paginator.return_value.paginate.return_value = [
        {"ResourceTagMappingList": [{"ResourceARN": arn}]}
    ]

    teardown.run(mock_clients(cloudformation=cloudformation, tagging=tagging), [1], dry_run=False)

    assert arn in capsys.readouterr().out.split("Ainda aparecem")[1]


def test_main_requires_explicit_mode() -> None:
    with pytest.raises(SystemExit) as raised:
        teardown.main(["--phase", "0"])
    assert raised.value.code == 2


def test_main_dry_run_returns_success(clients: AwsClients) -> None:
    with mock_aws():
        assert teardown.main(["--phase", "0", "--dry-run"]) == 0


def test_main_reports_failure_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise TeardownError("motivo claro")

    monkeypatch.setattr(teardown, "run", fail)
    monkeypatch.setattr(boto3, "Session", MagicMock())

    assert teardown.main(["--all", "--yes"]) == 1
    assert "motivo claro" in capsys.readouterr().err
