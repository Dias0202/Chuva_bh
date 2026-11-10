"""Lambda acionada pela aprovação no Model Registry: cria ou atualiza o Serverless Endpoint."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

import boto3
from botocore.exceptions import ClientError

from serving.store import ModelConfig

if TYPE_CHECKING:
    from types_boto3_sagemaker.client import SageMakerClient
    from types_boto3_sagemaker.type_defs import TagTypeDef
    from types_boto3_ssm.client import SSMClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SERVERLESS_MEMORY_MB: Final = 2048
SERVERLESS_MAX_CONCURRENCY: Final = 2
TAGS: Final[list[TagTypeDef]] = [
    {"Key": "Project", "Value": "chuva-bh"},
    {"Key": "Phase", "Value": "3"},
    {"Key": "Owner", "Value": "dias"},
]


def _already_exists(error: ClientError) -> bool:
    message = error.response.get("Error", {}).get("Message", "")
    return "already exist" in message


def _create_if_missing(action: Any, **kwargs: Any) -> None:
    """As chamadas são idempotentes: reprocessar o mesmo evento não duplica recursos."""
    try:
        action(**kwargs)
    except ClientError as error:
        if not _already_exists(error):
            raise


def deploy(
    event: Mapping[str, Any],
    *,
    sagemaker: SageMakerClient,
    ssm: SSMClient,
    role_arn: str,
    endpoint_name: str,
    parameter: str,
    group: str,
) -> ModelConfig | None:
    arn = event.get("detail", {}).get("ModelPackageArn")
    if not isinstance(arn, str):
        raise ValueError("evento sem ModelPackageArn")
    package = sagemaker.describe_model_package(ModelPackageName=arn)
    # O evento é só um gatilho: o estado vem da própria API.
    if package.get("ModelPackageGroupName") != group:
        logger.info("Pacote de outro grupo ignorado: %s", arn)
        return None
    if package.get("ModelApprovalStatus") != "Approved":
        logger.info("Pacote não aprovado ignorado: %s", arn)
        return None

    version = str(package["ModelPackageVersion"])
    name = f"{endpoint_name}-v{version}"
    _create_if_missing(
        sagemaker.create_model,
        ModelName=name,
        PrimaryContainer={"ModelPackageName": arn},
        ExecutionRoleArn=role_arn,
        Tags=TAGS,
    )
    _create_if_missing(
        sagemaker.create_endpoint_config,
        EndpointConfigName=name,
        ProductionVariants=[
            {
                "VariantName": "AllTraffic",
                "ModelName": name,
                "ServerlessConfig": {
                    "MemorySizeInMB": SERVERLESS_MEMORY_MB,
                    "MaxConcurrency": SERVERLESS_MAX_CONCURRENCY,
                },
            }
        ],
        Tags=TAGS,
    )
    try:
        current = sagemaker.describe_endpoint(EndpointName=endpoint_name)
    except ClientError as error:
        if "Could not find" not in error.response.get("Error", {}).get("Message", ""):
            raise
        sagemaker.create_endpoint(EndpointName=endpoint_name, EndpointConfigName=name, Tags=TAGS)
    else:
        if current.get("EndpointConfigName") != name:
            sagemaker.update_endpoint(EndpointName=endpoint_name, EndpointConfigName=name)

    metadata = package.get("CustomerMetadataProperties", {})
    config = ModelConfig(
        endpoint_name=endpoint_name,
        model_package_arn=arn,
        model_version=version,
        feature_set=metadata.get("feature_set", "full"),
    )
    ssm.put_parameter(Name=parameter, Value=config.to_json(), Type="String", Overwrite=True)
    logger.info("Endpoint %s apontando para a versão %s", endpoint_name, version)
    return config


def handler(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    config = deploy(
        event,
        sagemaker=boto3.client("sagemaker"),
        ssm=boto3.client("ssm"),
        role_arn=os.environ["SAGEMAKER_ROLE_ARN"],
        endpoint_name=os.environ["ENDPOINT_NAME"],
        parameter=os.environ["MODEL_PARAMETER"],
        group=os.environ["MODEL_PACKAGE_GROUP"],
    )
    return {"deployed": config.model_version if config else None}
