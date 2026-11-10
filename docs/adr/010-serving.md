# ADR-010: desenho do serving

Status: aceito em 2026-11-10.

## Decisões

- **Deploy na aprovação.** Uma regra do EventBridge escuta a aprovação no grupo
  `chuva-bh-rain` do Model Registry e aciona uma Lambda. A Lambda confere o estado do pacote
  pela API, cria modelo e configuração com o número da versão no nome e cria ou atualiza o
  Serverless Endpoint `chuva-bh-rain`. Reprocessar o mesmo evento não duplica recursos.
- **Configuração do modelo ativo no SSM.** O deployer grava em `/chuva-bh/serving/model` o
  endpoint, a versão e o conjunto de features. O scoring lê esse parâmetro a cada execução e
  não faz nada enquanto nenhum modelo foi aprovado.
- **Scoring depende da observação da hora.** Sem a chave da API do INMET, a observação chega
  com um mês de atraso, e o scoring registra os pontos como ignorados (`PointsSkipped`).
- **API com autenticação IAM.** A HTTP API só aceita chamadas assinadas por identidades da
  conta (o agente e a CLI), com limite de 5 requisições por segundo.
- **Empacotamento com uv.** A Lambda da API precisa do `pydantic`, que tem binário nativo. O
  `uv` baixa as wheels para Linux durante o `cdk synth`, sem Docker. Se o `uv` não estiver
  disponível, o CDK usa Docker com a imagem do runtime.
- **Teardown.** Endpoint, configurações e modelos são criados fora do CloudFormation, com as
  tags do projeto. O `teardown.py` os encontra pelas tags e os apaga antes das stacks.

## Alternativas descartadas

- Endpoint no CloudFormation: exigiria um deploy de stack a cada aprovação de modelo.
- API pública sem autenticação: expõe a conta a tráfego e custo sem necessidade.
