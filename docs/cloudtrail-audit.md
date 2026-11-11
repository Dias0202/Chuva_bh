# Consultas de auditoria no CloudTrail

A trail `chuva-bh-trail` registra os eventos de gerenciamento de todas as regiões. As consultas
abaixo usam o `lookup-events`, que cobre os últimos 90 dias sem custo.

## Quem aprovou um modelo

A aprovação no Model Registry é um `UpdateModelPackage` com `ModelApprovalStatus=Approved`.

```bash
aws cloudtrail lookup-events --region us-east-2 \
  --lookup-attributes AttributeKey=EventName,AttributeValue=UpdateModelPackage \
  --query 'Events[].{quando:EventTime,quem:Username,evento:CloudTrailEvent}'
```

## Quem implantou ou atualizou o endpoint

O deployer da Fase 3 cria e atualiza o endpoint com a role dele. Um nome de usuário diferente
indica uma alteração manual.

```bash
for evento in CreateEndpoint UpdateEndpoint DeleteEndpoint; do
  aws cloudtrail lookup-events --region us-east-2 \
    --lookup-attributes AttributeKey=EventName,AttributeValue=$evento \
    --query 'Events[].{quando:EventTime,quem:Username,evento:EventName}' --output table
done
```

## Quem fez deploy de stacks

```bash
aws cloudtrail lookup-events --region us-east-2 \
  --lookup-attributes AttributeKey=EventName,AttributeValue=ExecuteChangeSet \
  --query 'Events[].{quando:EventTime,quem:Username}' --output table
```

## Quando o kill switch disparou

O Budget Action anexa a política `chuva-bh-kill-switch-deny` à role de execução do CDK.

```bash
aws cloudtrail lookup-events --region us-east-1 \
  --lookup-attributes AttributeKey=EventName,AttributeValue=AttachRolePolicy \
  --query 'Events[].{quando:EventTime,quem:Username,evento:CloudTrailEvent}'
```

Eventos do IAM são globais e ficam registrados em us-east-1. Se essa região não estiver
acessível para a sua identidade, consulte os arquivos da trail no bucket `audit`.
