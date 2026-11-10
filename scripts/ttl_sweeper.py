#!/usr/bin/env python
"""Apaga recursos temporários do SageMaker cuja tag TTL (AAAA-MM-DD) já venceu.

Uso:
    ttl_sweeper.py              lista o que está vencido, sem alterar nada
    ttl_sweeper.py --yes        apaga schedules de monitoramento, endpoints, configs e modelos

Só considera recursos com a tag Project=chuva-bh.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from teardown import PROJECT, REGION, delete_sagemaker_resources, sagemaker_resources

if TYPE_CHECKING:
    from types_boto3_resourcegroupstaggingapi.client import ResourceGroupsTaggingAPIClient
    from types_boto3_sagemaker.client import SageMakerClient

RESOURCE_TYPES: Final = (
    "sagemaker:endpoint",
    "sagemaker:endpoint-config",
    "sagemaker:model",
    "sagemaker:monitoring-schedule",
)


@dataclass(frozen=True)
class Tagged:
    arn: str
    ttl: str


def list_tagged(tagging: ResourceGroupsTaggingAPIClient) -> list[Tagged]:
    paginator = tagging.get_paginator("get_resources")
    found = []
    for page in paginator.paginate(
        TagFilters=[{"Key": "Project", "Values": [PROJECT]}, {"Key": "TTL"}],
        ResourceTypeFilters=list(RESOURCE_TYPES),
    ):
        for mapping in page["ResourceTagMappingList"]:
            tags = {t["Key"]: t["Value"] for t in mapping.get("Tags", [])}
            found.append(Tagged(mapping["ResourceARN"], tags.get("TTL", "")))
    return found


def expired(resources: Iterable[Tagged], today: date) -> tuple[list[str], list[Tagged]]:
    """ARNs vencidos e recursos com TTL inválido, que ficam para revisão manual."""
    due, invalid = [], []
    for resource in resources:
        try:
            limit = date.fromisoformat(resource.ttl)
        except ValueError:
            invalid.append(resource)
            continue
        if limit <= today:
            due.append(resource.arn)
    return sorted(due), invalid


def delete_schedules(sagemaker: SageMakerClient, arns: Iterable[str]) -> None:
    for arn in arns:
        if ":monitoring-schedule/" in arn:
            name = arn.rsplit("/", 1)[-1]
            sagemaker.delete_monitoring_schedule(MonitoringScheduleName=name)
            print(f"Schedule {name} apagado")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="apaga os recursos vencidos")
    parser.add_argument("--profile")
    args = parser.parse_args(argv)

    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    clients: dict[str, Any] = {
        "tagging": session.client("resourcegroupstaggingapi"),
        "sagemaker": session.client("sagemaker"),
    }
    try:
        due, invalid = expired(list_tagged(clients["tagging"]), datetime.now(UTC).date())
        for resource in invalid:
            print(f"TTL inválido, revisar à mão: {resource.arn} ({resource.ttl!r})")
        if not due:
            print("Nada vencido.")
            return 0
        for arn in due:
            print(f"Vencido: {arn}")
        if not args.yes:
            print("Dry-run: nada foi alterado. Use --yes para apagar.")
            return 0
        delete_schedules(clients["sagemaker"], due)
        delete_sagemaker_resources(clients["sagemaker"], sagemaker_resources(due))
    except (ClientError, BotoCoreError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
