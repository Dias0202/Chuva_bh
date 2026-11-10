#!/usr/bin/env python
"""Remove os recursos AWS de uma fase do projeto, ou de todas.

Uso:
    teardown.py --phase 0 --dry-run     lista o que seria apagado
    teardown.py --phase 0 --yes         apaga
    teardown.py --all --yes             apaga todas as fases, da última para a primeira

Só atua em stacks com a tag Project=chuva-bh. A stack CDKToolkit do bootstrap não é
removida, pois não pertence a uma fase.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError, WaiterError

if TYPE_CHECKING:
    from types_boto3_cloudformation.client import CloudFormationClient
    from types_boto3_cloudtrail.client import CloudTrailClient
    from types_boto3_iam.client import IAMClient
    from types_boto3_resourcegroupstaggingapi.client import ResourceGroupsTaggingAPIClient
    from types_boto3_s3.client import S3Client
    from types_boto3_s3.type_defs import ObjectIdentifierTypeDef
    from types_boto3_sts.client import STSClient

logger = logging.getLogger("teardown")

PROJECT: Final = "chuva-bh"
REGION: Final = "us-east-2"
PHASE_STACKS: Final[dict[int, str]] = {
    0: f"{PROJECT}-foundation",
    1: f"{PROJECT}-ingestion",
    2: f"{PROJECT}-ml",
    3: f"{PROJECT}-serving",
    4: f"{PROJECT}-genai",
    5: f"{PROJECT}-observability",
}
FOUNDATION_PHASE: Final = 0

TYPE_BUCKET: Final = "AWS::S3::Bucket"
TYPE_MANAGED_POLICY: Final = "AWS::IAM::ManagedPolicy"
TYPE_TRAIL: Final = "AWS::CloudTrail::Trail"

S3_DELETE_BATCH_SIZE: Final = 1000
# A trail ainda pode entregar arquivos depois de parar, e isso faz a remoção do
# bucket falhar. Repetir o ciclo de esvaziar e apagar resolve esse caso.
MAX_DELETE_ATTEMPTS: Final = 3
WAITER_DELAY_SECONDS: Final = 15
WAITER_MAX_POLLS: Final = 80


class TeardownError(RuntimeError):
    """Falha esperada, com mensagem pronta para o operador."""


@dataclass(frozen=True)
class StackResource:
    resource_type: str
    logical_id: str
    physical_id: str


@dataclass(frozen=True)
class AwsClients:
    cloudformation: CloudFormationClient
    s3: S3Client
    iam: IAMClient
    cloudtrail: CloudTrailClient
    tagging: ResourceGroupsTaggingAPIClient
    sts: STSClient

    @classmethod
    def create(cls, profile: str | None = None) -> AwsClients:
        session = boto3.Session(profile_name=profile, region_name=REGION)
        return cls(
            cloudformation=session.client("cloudformation"),
            s3=session.client("s3"),
            iam=session.client("iam"),
            cloudtrail=session.client("cloudtrail"),
            tagging=session.client("resourcegroupstaggingapi"),
            sts=session.client("sts"),
        )


def error_code(error: ClientError) -> str:
    return error.response.get("Error", {}).get("Code", "")


def stack_tags(clients: AwsClients, stack_name: str) -> dict[str, str] | None:
    """Retorna as tags da stack, ou None se ela não existir."""
    try:
        stacks = clients.cloudformation.describe_stacks(StackName=stack_name)["Stacks"]
    except ClientError as error:
        if error_code(error) == "ValidationError" and "does not exist" in str(error):
            return None
        raise
    return {tag["Key"]: tag["Value"] for tag in stacks[0].get("Tags", [])}


def require_project_stack(clients: AwsClients, stack_name: str) -> bool:
    """Indica se a stack existe e recusa stacks que não sejam do projeto."""
    tags = stack_tags(clients, stack_name)
    if tags is None:
        return False
    if tags.get("Project") != PROJECT:
        raise TeardownError(
            f"A stack {stack_name} não tem a tag Project={PROJECT}; nada foi alterado."
        )
    return True


def list_stack_resources(clients: AwsClients, stack_name: str) -> list[StackResource]:
    paginator = clients.cloudformation.get_paginator("list_stack_resources")
    return [
        StackResource(
            resource_type=summary["ResourceType"],
            logical_id=summary["LogicalResourceId"],
            physical_id=summary.get("PhysicalResourceId", ""),
        )
        for page in paginator.paginate(StackName=stack_name)
        for summary in page["StackResourceSummaries"]
    ]


def list_tagged_arns(clients: AwsClients, phase: int | None) -> list[str]:
    """Lista os ARNs com as tags do projeto, restritos a uma fase quando informada."""
    filters = [{"Key": "Project", "Values": [PROJECT]}]
    if phase is not None:
        filters.append({"Key": "Phase", "Values": [str(phase)]})
    paginator = clients.tagging.get_paginator("get_resources")
    return sorted(
        mapping["ResourceARN"]
        for page in paginator.paginate(TagFilters=filters)  # type: ignore[arg-type]
        for mapping in page["ResourceTagMappingList"]
    )


def arns_outside_stacks(arns: Iterable[str], resources: Sequence[StackResource]) -> list[str]:
    """Filtra os ARNs que não correspondem a nenhum recurso das stacks listadas."""
    physical_ids = [resource.physical_id for resource in resources if resource.physical_id]
    return [arn for arn in arns if not any(physical in arn for physical in physical_ids)]


def physical_ids(resources: Iterable[StackResource], resource_type: str) -> list[str]:
    return [
        resource.physical_id
        for resource in resources
        if resource.resource_type == resource_type and resource.physical_id
    ]


def stop_trails(clients: AwsClients, trails: Iterable[str]) -> None:
    for trail in trails:
        try:
            clients.cloudtrail.stop_logging(Name=trail)
        except ClientError as error:
            if error_code(error) != "TrailNotFoundException":
                raise


def detach_policy(clients: AwsClients, policy_arn: str) -> None:
    """Desanexa a política de todas as entidades.

    O Budget Action anexa o deny do kill switch fora do CloudFormation, e uma
    política anexada impede a remoção da stack.
    """
    paginator = clients.iam.get_paginator("list_entities_for_policy")
    try:
        for page in paginator.paginate(PolicyArn=policy_arn):
            for role in page["PolicyRoles"]:
                clients.iam.detach_role_policy(RoleName=role["RoleName"], PolicyArn=policy_arn)
            for user in page["PolicyUsers"]:
                clients.iam.detach_user_policy(UserName=user["UserName"], PolicyArn=policy_arn)
            for group in page["PolicyGroups"]:
                clients.iam.detach_group_policy(GroupName=group["GroupName"], PolicyArn=policy_arn)
    except ClientError as error:
        if error_code(error) != "NoSuchEntity":
            raise


def empty_bucket(clients: AwsClients, bucket: str) -> int:
    """Apaga todos os objetos, versões e marcadores. Retorna a quantidade removida."""
    removed = 0
    paginator = clients.s3.get_paginator("list_object_versions")
    try:
        for page in paginator.paginate(Bucket=bucket):
            entries = [*page.get("Versions", []), *page.get("DeleteMarkers", [])]
            identifiers: list[ObjectIdentifierTypeDef] = [
                {"Key": entry["Key"], "VersionId": entry["VersionId"]} for entry in entries
            ]
            for start in range(0, len(identifiers), S3_DELETE_BATCH_SIZE):
                batch = identifiers[start : start + S3_DELETE_BATCH_SIZE]
                response = clients.s3.delete_objects(
                    Bucket=bucket, Delete={"Objects": batch, "Quiet": True}
                )
                if response.get("Errors"):
                    raise TeardownError(
                        f"Falha ao apagar objetos de {bucket}: {response['Errors'][:3]}"
                    )
                removed += len(batch)
    except ClientError as error:
        if error_code(error) != "NoSuchBucket":
            raise
    return removed


def delete_stack(clients: AwsClients, stack_name: str) -> None:
    resources = list_stack_resources(clients, stack_name)
    stop_trails(clients, physical_ids(resources, TYPE_TRAIL))
    for policy_arn in physical_ids(resources, TYPE_MANAGED_POLICY):
        detach_policy(clients, policy_arn)

    waiter = clients.cloudformation.get_waiter("stack_delete_complete")
    last_error: WaiterError | None = None
    for attempt in range(1, MAX_DELETE_ATTEMPTS + 1):
        for bucket in physical_ids(resources, TYPE_BUCKET):
            removed = empty_bucket(clients, bucket)
            logger.info("Bucket %s: %d objetos removidos", bucket, removed)
        clients.cloudformation.delete_stack(StackName=stack_name)
        try:
            waiter.wait(
                StackName=stack_name,
                WaiterConfig={"Delay": WAITER_DELAY_SECONDS, "MaxAttempts": WAITER_MAX_POLLS},
            )
        except WaiterError as error:
            last_error = error
            logger.warning(
                "Tentativa %d/%d de apagar %s falhou", attempt, MAX_DELETE_ATTEMPTS, stack_name
            )
        else:
            logger.info("Stack %s apagada", stack_name)
            return
    raise TeardownError(
        f"Não foi possível apagar {stack_name} após {MAX_DELETE_ATTEMPTS} tentativas: {last_error}"
    )


def select_phases(phase: int | None, all_phases: bool) -> list[int]:
    """Define as fases alvo, sempre da mais recente para a mais antiga."""
    if all_phases:
        return sorted(PHASE_STACKS, reverse=True)
    if phase is None:
        raise TeardownError("Informe --phase ou --all.")
    return [phase]


def existing_stacks(clients: AwsClients, phases: Iterable[int]) -> list[tuple[int, str]]:
    found = []
    for phase in phases:
        stack_name = PHASE_STACKS[phase]
        if require_project_stack(clients, stack_name):
            found.append((phase, stack_name))
    return found


def guard_foundation_dependents(clients: AwsClients, phases: Sequence[int]) -> None:
    """Impede remover a fundação enquanto houver fases que dependem dela."""
    if phases != [FOUNDATION_PHASE]:
        return
    later = [phase for phase in PHASE_STACKS if phase != FOUNDATION_PHASE]
    dependents = [name for _, name in existing_stacks(clients, later)]
    if dependents:
        raise TeardownError(
            "A fundação é usada pelas stacks "
            f"{', '.join(dependents)}. Remova-as antes ou use --all."
        )


def print_plan(
    targets: Sequence[tuple[int, str]],
    resources_by_stack: dict[str, list[StackResource]],
    orphan_arns: Sequence[str],
) -> None:
    if not targets:
        print("Nenhuma stack do projeto encontrada para as fases selecionadas.")
    for phase, stack_name in targets:
        resources = resources_by_stack[stack_name]
        print(f"\nFase {phase}: stack {stack_name} ({len(resources)} recursos)")
        for resource in sorted(resources, key=lambda r: (r.resource_type, r.logical_id)):
            print(
                f"  {resource.resource_type:<36} {resource.logical_id:<44} {resource.physical_id}"
            )
        for trail in physical_ids(resources, TYPE_TRAIL):
            print(f"  -> parar a trail {trail}")
        for bucket in physical_ids(resources, TYPE_BUCKET):
            print(f"  -> esvaziar o bucket {bucket}")
        for policy in physical_ids(resources, TYPE_MANAGED_POLICY):
            print(f"  -> desanexar a política {policy}")
        print(f"  -> apagar a stack {stack_name}")
    if orphan_arns:
        print("\nRecursos com as tags do projeto fora das stacks (remoção manual):")
        for arn in orphan_arns:
            print(f"  {arn}")


def run(clients: AwsClients, phases: Sequence[int], *, dry_run: bool) -> None:
    guard_foundation_dependents(clients, phases)
    targets = existing_stacks(clients, phases)
    resources_by_stack = {
        stack_name: list_stack_resources(clients, stack_name) for _, stack_name in targets
    }
    all_resources = [r for resources in resources_by_stack.values() for r in resources]
    scope = phases[0] if len(phases) == 1 else None

    print(f"Conta {clients.sts.get_caller_identity()['Account']}, região {REGION}")
    print_plan(
        targets,
        resources_by_stack,
        arns_outside_stacks(list_tagged_arns(clients, scope), all_resources),
    )
    if dry_run:
        print("\nDry-run: nada foi alterado.")
        return

    for _, stack_name in targets:
        delete_stack(clients, stack_name)

    leftovers = list_tagged_arns(clients, scope)
    if leftovers:
        print(
            "\nAinda aparecem com as tags do projeto (chaves KMS em exclusão agendada são normais):"
        )
        for arn in leftovers:
            print(f"  {arn}")
    print("\nTeardown concluído.")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--phase", type=int, choices=sorted(PHASE_STACKS))
    scope.add_argument("--all", action="store_true", dest="all_phases")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="lista sem alterar nada")
    mode.add_argument("--yes", action="store_true", help="confirma a remoção")
    parser.add_argument("--profile", help="perfil AWS a usar")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        phases = select_phases(args.phase, args.all_phases)
        run(AwsClients.create(args.profile), phases, dry_run=args.dry_run)
    except (TeardownError, ClientError, BotoCoreError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
