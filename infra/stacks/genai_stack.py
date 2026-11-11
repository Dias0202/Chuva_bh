"""Fase 4: Knowledge Base em S3 Vectors, Guardrail, Prompt Management e AgentCore Runtime."""

from __future__ import annotations

from typing import Any, Final

from aws_cdk import CfnOutput, DockerImage, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import aws_s3vectors as s3vectors
from constructs import Construct

from agent.constants import DEFAULT_MODEL_ID, PROMPT_FILE
from stacks.bundling import ARM64, bundling_options
from stacks.common import PROJECT, apply_project_tags
from stacks.ingestion_stack import SOURCE_DIR

PHASE: Final = "4"
EMBEDDING_MODEL: Final = "amazon.titan-embed-text-v2:0"
EMBEDDING_DIMENSIONS: Final = 1024
AGENT_MODEL: Final = "amazon.nova-lite-v1:0"
KB_PREFIX: Final = "docs/"
RUNTIME_NAME: Final = "chuva_bh_agent"
AGENT_REQUIREMENTS: Final = (
    "bedrock-agentcore==1.25.4",
    "langchain-aws==1.9.2",
    "langgraph==1.2.20",
    "pydantic==2.14.3",
)
CHUNKING_STRATEGIES: Final = ("fixed", "hierarchical")
# ADDRESS e NAME ficaram de fora: o filtro trocava nomes de cidade e bairro, que são o
# conteúdo normal das respostas, por marcadores como {ADDRESS}.
PII_ENTITIES: Final = ("EMAIL", "PHONE")
OFF_TOPIC_MESSAGE: Final = (
    "Só posso ajudar com chuva e tempo nos pontos monitorados da região de Belo Horizonte."
)


class GenAiStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        kb_bucket: s3.IBucket,
        key: kms.IKey,
        api: apigw.HttpApi,
        chunking: str = "fixed",
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if chunking not in CHUNKING_STRATEGIES:
            raise ValueError(f"chunking deve ser um de {CHUNKING_STRATEGIES}")

        knowledge_base = self._create_knowledge_base(kb_bucket, key, chunking)
        guardrail, guardrail_version = self._create_guardrail()
        prompt, prompt_version = self._create_prompt()
        runtime = self._create_runtime(
            api, knowledge_base, guardrail, guardrail_version, prompt, prompt_version
        )
        self._create_invocation_logging()

        apply_project_tags(self, PHASE)
        CfnOutput(self, "KnowledgeBaseId", value=knowledge_base.attr_knowledge_base_id)
        CfnOutput(self, "GuardrailId", value=guardrail.attr_guardrail_id)
        CfnOutput(self, "GuardrailVersion", value=guardrail_version.attr_version)
        CfnOutput(self, "PromptId", value=prompt.attr_id)
        CfnOutput(self, "PromptVersion", value=prompt_version.attr_version)
        CfnOutput(self, "AgentRuntimeArn", value=runtime.attr_agent_runtime_arn)

    def _arn(self, service: str, resource: str, region: str | None = None) -> str:
        return f"arn:{self.partition}:{service}:{region or self.region}:{self.account}:{resource}"

    def _create_knowledge_base(
        self, kb_bucket: s3.IBucket, key: kms.IKey, chunking: str
    ) -> bedrock.CfnKnowledgeBase:
        vector_bucket = s3vectors.CfnVectorBucket(
            self, "VectorBucket", vector_bucket_name=f"{PROJECT}-kb"
        )
        index = s3vectors.CfnIndex(
            self,
            "VectorIndex",
            index_name="docs",
            vector_bucket_name=f"{PROJECT}-kb",
            data_type="float32",
            dimension=EMBEDDING_DIMENSIONS,
            distance_metric="cosine",
            # O Bedrock grava o texto do trecho nesses campos, grandes demais para filtro.
            metadata_configuration=s3vectors.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]
            ),
        )
        index.add_dependency(vector_bucket)

        role = iam.Role(
            self,
            "KnowledgeBaseRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
        )
        kb_bucket.grant_read(role, f"{KB_PREFIX}*")
        key.grant_decrypt(role)
        embedding_arn = (
            f"arn:{self.partition}:bedrock:{self.region}::foundation-model/{EMBEDDING_MODEL}"
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="Embed", actions=["bedrock:InvokeModel"], resources=[embedding_arn]
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="VectorStore",
                actions=[
                    "s3vectors:DeleteVectors",
                    "s3vectors:GetIndex",
                    "s3vectors:GetVectors",
                    "s3vectors:ListVectors",
                    "s3vectors:PutVectors",
                    "s3vectors:QueryVectors",
                ],
                resources=[index.attr_index_arn],
            )
        )

        knowledge_base = bedrock.CfnKnowledgeBase(
            self,
            "KnowledgeBase",
            name=f"{PROJECT}-docs",
            role_arn=role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=embedding_arn,
                    embedding_model_configuration=bedrock.CfnKnowledgeBase.EmbeddingModelConfigurationProperty(
                        bedrock_embedding_model_configuration=bedrock.CfnKnowledgeBase.BedrockEmbeddingModelConfigurationProperty(
                            dimensions=EMBEDDING_DIMENSIONS, embedding_data_type="FLOAT32"
                        )
                    ),
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="S3_VECTORS",
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                    index_arn=index.attr_index_arn,
                    vector_bucket_arn=vector_bucket.attr_vector_bucket_arn,
                ),
            ),
        )
        knowledge_base.node.add_dependency(role)

        if chunking == "fixed":
            chunking_config = bedrock.CfnDataSource.ChunkingConfigurationProperty(
                chunking_strategy="FIXED_SIZE",
                fixed_size_chunking_configuration=bedrock.CfnDataSource.FixedSizeChunkingConfigurationProperty(
                    max_tokens=300, overlap_percentage=20
                ),
            )
        else:
            chunking_config = bedrock.CfnDataSource.ChunkingConfigurationProperty(
                chunking_strategy="HIERARCHICAL",
                hierarchical_chunking_configuration=bedrock.CfnDataSource.HierarchicalChunkingConfigurationProperty(
                    level_configurations=[
                        bedrock.CfnDataSource.HierarchicalChunkingLevelConfigurationProperty(
                            max_tokens=1000
                        ),
                        bedrock.CfnDataSource.HierarchicalChunkingLevelConfigurationProperty(
                            max_tokens=250
                        ),
                    ],
                    overlap_tokens=50,
                ),
            )
        bedrock.CfnDataSource(
            self,
            "DocsDataSource",
            name=f"{PROJECT}-docs-{chunking}",
            knowledge_base_id=knowledge_base.attr_knowledge_base_id,
            data_deletion_policy="DELETE",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=kb_bucket.bucket_arn, inclusion_prefixes=[KB_PREFIX]
                ),
            ),
            vector_ingestion_configuration=bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
                chunking_configuration=chunking_config
            ),
        )
        return knowledge_base

    def _create_guardrail(self) -> tuple[bedrock.CfnGuardrail, bedrock.CfnGuardrailVersion]:
        strong = "HIGH"
        filters = [
            bedrock.CfnGuardrail.ContentFilterConfigProperty(
                type=kind, input_strength=strong, output_strength=strong
            )
            for kind in ("HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT")
        ]
        filters.append(
            bedrock.CfnGuardrail.ContentFilterConfigProperty(
                type="PROMPT_ATTACK", input_strength=strong, output_strength="NONE"
            )
        )
        guardrail = bedrock.CfnGuardrail(
            self,
            "Guardrail",
            name=f"{PROJECT}-agent",
            description="Mantém o agente no tema de chuva e protege dados pessoais",
            blocked_input_messaging=OFF_TOPIC_MESSAGE,
            blocked_outputs_messaging=OFF_TOPIC_MESSAGE,
            # Sem tópico negado: no nível clássico os tópicos são treinados para inglês e
            # bloqueavam perguntas legítimas sobre chuva em português. A recusa fora do tema
            # fica com o prompt de sistema e é medida pelo golden set.
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=filters
            ),
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(type=kind, action="ANONYMIZE")
                    for kind in PII_ENTITIES
                ]
            ),
        )
        # A versão publicada é um retrato do guardrail. A descrição muda junto com a
        # configuração e força uma versão nova a cada alteração.
        version = bedrock.CfnGuardrailVersion(
            self,
            "GuardrailPublishedVersion",
            guardrail_identifier=guardrail.attr_guardrail_id,
            description=f"Sem tópico negado; PII mascarada: {', '.join(PII_ENTITIES)}",
        )
        return guardrail, version

    def _create_prompt(self) -> tuple[bedrock.CfnPrompt, bedrock.CfnPromptVersion]:
        prompt = bedrock.CfnPrompt(
            self,
            "SystemPrompt",
            name=f"{PROJECT}-system",
            description="Prompt de sistema do agente; a origem é src/agent/system_prompt.md",
            default_variant="default",
            variants=[
                bedrock.CfnPrompt.PromptVariantProperty(
                    name="default",
                    template_type="TEXT",
                    model_id=AGENT_MODEL,
                    template_configuration=bedrock.CfnPrompt.PromptTemplateConfigurationProperty(
                        text=bedrock.CfnPrompt.TextPromptTemplateConfigurationProperty(
                            text=PROMPT_FILE.read_text(encoding="utf-8")
                        )
                    ),
                )
            ],
        )
        version = bedrock.CfnPromptVersion(self, "SystemPromptVersion", prompt_arn=prompt.attr_arn)
        return prompt, version

    def _create_runtime(
        self,
        api: apigw.HttpApi,
        knowledge_base: bedrock.CfnKnowledgeBase,
        guardrail: bedrock.CfnGuardrail,
        guardrail_version: bedrock.CfnGuardrailVersion,
        prompt: bedrock.CfnPrompt,
        prompt_version: bedrock.CfnPromptVersion,
    ) -> agentcore.CfnRuntime:
        code = s3_assets.Asset(
            self,
            "AgentCode",
            path=str(SOURCE_DIR),
            bundling=bundling_options(
                SOURCE_DIR,
                packages=("agent", "agent_main.py"),
                requirements=AGENT_REQUIREMENTS,
                platform=ARM64,
                image=DockerImage.from_registry("public.ecr.aws/sam/build-python3.12:latest-arm64"),
            ),
        )
        role = iam.Role(
            self,
            "AgentRuntimeRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
        )
        code.grant_read(role)
        statements = {
            "InvokeAgentModel": (
                ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                [
                    f"arn:{self.partition}:bedrock:*::foundation-model/{AGENT_MODEL}",
                    self._arn("bedrock", f"inference-profile/{DEFAULT_MODEL_ID}"),
                ],
            ),
            "ApplyGuardrail": (["bedrock:ApplyGuardrail"], [guardrail.attr_guardrail_arn]),
            "RetrieveDocs": (["bedrock:Retrieve"], [knowledge_base.attr_knowledge_base_arn]),
            "ReadPrompt": (["bedrock:GetPrompt"], [prompt.attr_arn, f"{prompt.attr_arn}:*"]),
            "CallForecastApi": (
                ["execute-api:Invoke"],
                [api.arn_for_execute_api(method="GET", path="/*")],
            ),
            "RuntimeLogs": (
                ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                [self._arn("logs", "log-group:/aws/bedrock-agentcore/runtimes/*")],
            ),
        }
        for sid, (actions, resources) in statements.items():
            role.add_to_policy(iam.PolicyStatement(sid=sid, actions=actions, resources=resources))
        # X-Ray não aceita escopo por recurso; as métricas são limitadas pelo namespace.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="Traces",
                actions=["xray:PutTelemetryRecords", "xray:PutTraceSegments"],
                resources=["*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="RuntimeMetrics",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
            )
        )

        runtime = agentcore.CfnRuntime(
            self,
            "AgentRuntime",
            agent_runtime_name=RUNTIME_NAME,
            description="Agente de previsão de chuva do chuva-bh",
            role_arn=role.role_arn,
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                code_configuration=agentcore.CfnRuntime.CodeConfigurationProperty(
                    code=agentcore.CfnRuntime.CodeProperty(
                        s3=agentcore.CfnRuntime.S3LocationProperty(
                            bucket=code.s3_bucket_name, prefix=code.s3_object_key
                        )
                    ),
                    entry_point=["agent_main.py"],
                    runtime="PYTHON_3_12",
                )
            ),
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="PUBLIC"
            ),
            environment_variables={
                "FORECAST_API_URL": api.api_endpoint,
                "KNOWLEDGE_BASE_ID": knowledge_base.attr_knowledge_base_id,
                "GUARDRAIL_ID": guardrail.attr_guardrail_id,
                "GUARDRAIL_VERSION": guardrail_version.attr_version,
                "PROMPT_ID": prompt.attr_id,
                "PROMPT_VERSION": prompt_version.attr_version,
                "MODEL_ID": DEFAULT_MODEL_ID,
            },
        )
        runtime.node.add_dependency(role)
        # O runtime cria o próprio log group ao nascer, com um nome que leva o ID dele. A
        # retenção de 14 dias é aplicada depois do deploy por `genai_ops.py enable-logging`.
        return runtime

    def _create_invocation_logging(self) -> None:
        """Destino do invocation logging do Bedrock, ligado por script na conta."""
        group = logs.LogGroup(
            self,
            "BedrockInvocationLogGroup",
            log_group_name=f"/{PROJECT}/bedrock-invocations",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        role = iam.Role(
            self,
            "BedrockLoggingRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
        )
        group.grant_write(role)
        CfnOutput(self, "InvocationLogGroupName", value=group.log_group_name)
        CfnOutput(self, "InvocationLoggingRoleArn", value=role.role_arn)
