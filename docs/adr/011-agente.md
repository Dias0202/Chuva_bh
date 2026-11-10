# ADR-011: agente no AgentCore Runtime

Status: aceito em 2026-11-10, com riscos a confirmar no primeiro deploy.

## Decisões

- **Grafo próprio no LangGraph.** Um nó de modelo e um `ToolNode`, com limite de 10 passos
  por pergunta (no máximo quatro chamadas de ferramenta). Erros de argumento voltam ao modelo
  como mensagem, sem derrubar a execução.
- **Três ferramentas, todas de leitura.** `get_forecast` e `list_points` chamam a API da
  Fase 3 com assinatura SigV4. `search_docs` usa o Retrieve da Knowledge Base. Os argumentos
  são validados por schemas `pydantic` com `extra="forbid"`.
- **Aviso fixo.** O texto sobre os alertas oficiais é acrescentado pelo código a toda
  resposta, sem depender do modelo.
- **Prompt versionado.** `src/agent/system_prompt.md` é a origem. O CDK publica esse texto no
  Prompt Management, e o agente lê a versão publicada; sem ela, usa o arquivo.
- **Knowledge Base em S3 Vectors**, com Titan Text Embeddings V2 (1024 dimensões). A
  estratégia de chunking se escolhe com `-c kb_chunking=fixed|hierarchical`, para comparar as
  duas no golden set.
- **Código, não contêiner.** O AgentCore Runtime recebe um zip em S3 com as dependências para
  Linux ARM64, empacotado pelo `uv` (ADR-010). Não há imagem para manter no ECR.
- **Corpus próprio.** A base usa só textos escritos para o projeto (`docs/kb`) e o model card,
  o que evita pendências de licença.

## Riscos a confirmar no primeiro deploy

- **SCP e inferência entre regiões.** O modelo padrão é o perfil `us.amazon.nova-lite-v1:0`,
  que pode rotear para us-east-1, região negada pela SCP da conta. Se a chamada falhar, use um
  modelo com inferência na própria região por `MODEL_ID` ou peça a exceção na SCP.
- **Disponibilidade em us-east-2.** Titan Text Embeddings V2, S3 Vectors e AgentCore Runtime
  precisam estar disponíveis na região para esta conta.
- **Guardrail em português.** As definições do tópico negado estão em inglês, com exemplos em
  português. A eficácia em português precisa ser medida pelos casos de recusa do golden set.
- **Tamanho do pacote.** O zip do agente tem cerca de 131 MB descompactado, dentro do limite
  esperado para código direto, mas o valor deve ser conferido na primeira publicação.
- **Observabilidade.** Os traces do AgentCore exigem o CloudWatch Transaction Search ligado na
  conta, uma configuração única feita no console.
