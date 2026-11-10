"""Avaliação do agente: acerto de ferramenta, recusa correta e fidelidade às fontes."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent.graph import AgentAnswer

TOOL_ACCURACY_TARGET: Final = 0.85
FAITHFULNESS_TARGET: Final = 0.8
_REFUSAL_MARKERS: Final = (
    "não posso",
    "não consigo ajudar",
    "fora do escopo",
    "só posso",
    "apenas sobre",
    "não trato",
)

JUDGE_PROMPT: Final = """Você avalia se uma resposta é fiel ao contexto fornecido.
Pontue de 0 a 1: 1 quando toda afirmação factual da resposta é sustentada pelo contexto,
0 quando a resposta contradiz ou inventa fatos. Ignore o aviso padrão sobre alertas oficiais.
Responda apenas com JSON no formato {{"score": <número>, "reason": "<frase curta>"}}.

Contexto:
<contexto>
{context}
</contexto>

Resposta:
<resposta>
{answer}
</resposta>"""


class GoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    question: str = Field(min_length=3)
    category: str
    expected_tools: list[str] = Field(default_factory=list)
    should_refuse: bool = False


class JudgeVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    score: float = Field(ge=0.0, le=1.0)
    reason: str = ""


@dataclass
class CaseResult:
    case: GoldenCase
    tools: list[str]
    tool_correct: bool
    refusal_correct: bool
    faithfulness: float | None
    error: str | None = None


@dataclass
class Report:
    results: list[CaseResult] = field(default_factory=list)

    def _rate(self, values: Iterable[bool]) -> float:
        items = list(values)
        return sum(items) / len(items) if items else float("nan")

    @property
    def tool_accuracy(self) -> float:
        return self._rate(r.tool_correct for r in self.results if not r.case.should_refuse)

    @property
    def refusal_accuracy(self) -> float:
        return self._rate(r.refusal_correct for r in self.results if r.case.should_refuse)

    @property
    def faithfulness(self) -> float:
        scores = [r.faithfulness for r in self.results if r.faithfulness is not None]
        return sum(scores) / len(scores) if scores else float("nan")

    def summary(self) -> dict[str, Any]:
        return {
            "cases": len(self.results),
            "errors": sum(1 for r in self.results if r.error),
            "tool_accuracy": self.tool_accuracy,
            "refusal_accuracy": self.refusal_accuracy,
            "faithfulness": self.faithfulness,
            "meets_targets": self.tool_accuracy >= TOOL_ACCURACY_TARGET
            and self.faithfulness >= FAITHFULNESS_TARGET,
        }


def load_golden_set(lines: Iterable[str]) -> list[GoldenCase]:
    cases = [GoldenCase.model_validate_json(line) for line in lines if line.strip()]
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("ids repetidos no golden set")
    return cases


def tools_match(expected: list[str], called: list[str]) -> bool:
    """Todas as ferramentas esperadas foram chamadas; sem esperadas, nenhuma deveria ser."""
    if not expected:
        return not called
    return set(expected) <= set(called)


def looks_like_refusal(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _REFUSAL_MARKERS)


def parse_verdict(raw: str) -> JudgeVerdict | None:
    """Saída do juiz é entrada não confiável: só vale se for JSON válido no schema."""
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if match is None:
        return None
    try:
        return JudgeVerdict.model_validate(json.loads(match.group(0)))
    except (json.JSONDecodeError, ValidationError):
        return None


def evaluate(
    cases: Iterable[GoldenCase],
    agent: Callable[[str], AgentAnswer],
    judge: Callable[[str], str] | None,
) -> Report:
    report = Report()
    for case in cases:
        try:
            result = agent(case.question)
        except Exception as error:
            report.results.append(CaseResult(case, [], False, False, None, error=str(error)))
            continue
        faithfulness = None
        if judge is not None and result.contexts and not case.should_refuse:
            verdict = parse_verdict(
                judge(JUDGE_PROMPT.format(context="\n".join(result.contexts), answer=result.text))
            )
            faithfulness = verdict.score if verdict else None
        report.results.append(
            CaseResult(
                case=case,
                tools=result.tools,
                tool_correct=tools_match(case.expected_tools, result.tools),
                refusal_correct=looks_like_refusal(result.text) == case.should_refuse,
                faithfulness=faithfulness,
            )
        )
    return report
