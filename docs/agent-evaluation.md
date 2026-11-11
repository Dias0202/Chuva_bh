# Avaliação do agente

Golden set de 36 casos (`eval/golden_set.jsonl`) rodado em 2026-11-11 com `eval/run_eval.py`,
usando os recursos implantados: Knowledge Base, Guardrail, prompt publicado e a API da Fase 3.
O juiz de fidelidade é o Amazon Nova Pro. As metas são 85% de acerto de ferramenta e 0,8 de
fidelidade.

## Guardrail

| Configuração | Acerto de ferramenta | Recusa correta | Fidelidade |
|---|---:|---:|---:|
| Com tópico negado "fora do tema" | 57% | 100% | 1,00 (16 casos julgados) |
| Sem tópico negado | **89%** | 88% | **0,96** (26 casos julgados) |

O tópico negado bloqueava como fora do tema perguntas legítimas em português, como "Florestal
vai ter chuva hoje à tarde?". O diagnóstico com `ApplyGuardrail` apontou só essa política. Sem
ela, a recusa depende do prompt de sistema; o caso que escapou foi a cotação do dólar.

## Chunking da Knowledge Base

| Estratégia | Acerto de ferramenta | Recusa correta | Fidelidade |
|---|---:|---:|---:|
| Fixo, 300 tokens com 20% de sobreposição | 89% | 88% | **0,96** |
| Hierárquico, 1000 e 250 tokens | 93% | 100% | 0,91 |

O chunking muda só o que a busca devolve, e a métrica ligada a isso é a fidelidade, melhor no
chunking fixo. As diferenças de ferramenta e de recusa vêm da decisão do modelo e variam entre
execuções. O chunking fixo ficou em produção.

## Erros restantes (chunking fixo)

- "Florestal vai ter chuva hoje à tarde?": o agente listou os pontos e pediu o horário em vez
  de consultar a previsão.
- "Vocês têm previsão para algum ponto fora de Belo Horizonte?" e "A previsão de vocês
  substitui os alertas da Defesa Civil?": respondeu sem consultar as ferramentas.
- "Qual a cotação do dólar hoje?": não recusou.
