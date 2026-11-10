"""Lambda agendada pelo EventBridge Scheduler, com DLQ e log de 14 dias.

A IngestionStack tem uma cópia anterior deste padrão; ela não usa o helper para
não mudar os IDs lógicos de recursos já implantados.
"""

from __future__ import annotations

import json
from typing import Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sqs as sqs
from constructs import Construct

RETRY_ATTEMPTS: Final = 2


def dead_letter_queue(scope: Construct, construct_id: str, name: str) -> sqs.Queue:
    return sqs.Queue(
        scope,
        construct_id,
        queue_name=name,
        encryption=sqs.QueueEncryption.SQS_MANAGED,
        enforce_ssl=True,
        retention_period=Duration.days(14),
    )


def log_group(scope: Construct, construct_id: str, function_name: str) -> logs.LogGroup:
    return logs.LogGroup(
        scope,
        construct_id,
        log_group_name=f"/aws/lambda/{function_name}",
        retention=logs.RetentionDays.TWO_WEEKS,
        removal_policy=RemovalPolicy.DESTROY,
    )


def schedule(
    scope: Construct,
    construct_id: str,
    *,
    name: str,
    expression: str,
    function: lambda_.IFunction,
    queue: sqs.IQueue,
) -> scheduler.CfnSchedule:
    role = iam.Role(
        scope,
        f"{construct_id}Role",
        assumed_by=iam.ServicePrincipal(
            "scheduler.amazonaws.com",
            conditions={"StringEquals": {"aws:SourceAccount": Stack.of(scope).account}},
        ),
    )
    function.grant_invoke(role)
    queue.grant_send_messages(role)
    return scheduler.CfnSchedule(
        scope,
        construct_id,
        name=name,
        schedule_expression=expression,
        schedule_expression_timezone="UTC",
        flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
        target=scheduler.CfnSchedule.TargetProperty(
            arn=function.function_arn,
            role_arn=role.role_arn,
            input=json.dumps({"scheduled_time": "<aws.scheduler.scheduled-time>"}),
            retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                maximum_retry_attempts=RETRY_ATTEMPTS, maximum_event_age_in_seconds=3600
            ),
            dead_letter_config=scheduler.CfnSchedule.DeadLetterConfigProperty(arn=queue.queue_arn),
        ),
    )
