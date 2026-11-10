#!/usr/bin/env python
"""Gera o relatório de custo do projeto e o registra em docs/cost-log.md.

Cada requisição ao Cost Explorer custa US$ 0,01, e uma execução faz duas, ou mais
se a resposta vier paginada. Os valores são brutos: créditos e reembolsos ficam de
fora, senão o custo apareceria zerado enquanto houver crédito na conta.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from types_boto3_ce.client import CostExplorerClient
    from types_boto3_ce.literals import GroupDefinitionTypeType
    from types_boto3_ce.type_defs import (
        ExpressionTypeDef,
        GetCostAndUsageRequestTypeDef,
        GroupDefinitionTypeDef,
    )

PROJECT: Final = "chuva-bh"
PROJECT_START: Final = date(2026, 11, 1)
BUDGET_USD: Final = Decimal("60")
METRIC: Final = "UnblendedCost"
EXCLUDED_RECORD_TYPES: Final = ("Credit", "Refund")
MAX_PAGES: Final = 5
DEFAULT_LOG_PATH: Final = Path(__file__).resolve().parent.parent / "docs" / "cost-log.md"
LOG_HEADER: Final = "# Registro de custos\n"
UNTAGGED_LABEL: Final = "(sem tag Phase)"


class CostReportError(RuntimeError):
    """Falha esperada, com mensagem pronta para o operador."""


@dataclass(frozen=True)
class CostReport:
    run_date: date
    start: date
    end: date
    by_service: dict[str, Decimal]
    by_phase: dict[str, Decimal]

    @property
    def account_total(self) -> Decimal:
        return sum(self.by_service.values(), Decimal(0))

    @property
    def budget_used_percent(self) -> Decimal:
        return (self.account_total / BUDGET_USD * 100).quantize(Decimal("0.1"))


def gross_cost_filter(extra: ExpressionTypeDef | None = None) -> ExpressionTypeDef:
    """Monta o filtro que exclui créditos e reembolsos."""
    no_credits: ExpressionTypeDef = {
        "Not": {
            "Dimensions": {
                "Key": "RECORD_TYPE",
                "Values": list(EXCLUDED_RECORD_TYPES),
            }
        }
    }
    return {"And": [no_credits, extra]} if extra else no_credits


def project_filter() -> ExpressionTypeDef:
    return gross_cost_filter({"Tags": {"Key": "Project", "Values": [PROJECT]}})


def fetch_costs(
    ce: CostExplorerClient,
    start: date,
    end: date,
    group_type: GroupDefinitionTypeType,
    group_key: str,
    expression: ExpressionTypeDef,
) -> dict[str, Decimal]:
    """Soma o custo por grupo em todo o período, com número limitado de páginas."""
    totals: dict[str, Decimal] = defaultdict(Decimal)
    group_by: list[GroupDefinitionTypeDef] = [{"Type": group_type, "Key": group_key}]
    request: GetCostAndUsageRequestTypeDef = {
        "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
        "Granularity": "MONTHLY",
        "Metrics": [METRIC],
        "GroupBy": group_by,
        "Filter": expression,
    }
    for _ in range(MAX_PAGES):
        response = ce.get_cost_and_usage(**request)
        for period in response["ResultsByTime"]:
            for group in period.get("Groups", []):
                totals[group["Keys"][0]] += Decimal(group["Metrics"][METRIC]["Amount"])
        token = response.get("NextPageToken")
        if not token:
            return dict(totals)
        request["NextPageToken"] = token
    raise CostReportError(f"Resposta do Cost Explorer excedeu {MAX_PAGES} páginas.")


def phase_label(tag_value: str) -> str:
    """Converte a chave 'Phase$0' devolvida pelo Cost Explorer em rótulo legível."""
    value = tag_value.partition("$")[2]
    return f"Fase {value}" if value else UNTAGGED_LABEL


def build_report(ce: CostExplorerClient, run_date: date, start: date, end: date) -> CostReport:
    by_service = fetch_costs(ce, start, end, "DIMENSION", "SERVICE", gross_cost_filter())
    raw_phases = fetch_costs(ce, start, end, "TAG", "Phase", project_filter())
    return CostReport(
        run_date=run_date,
        start=start,
        end=end,
        by_service=by_service,
        by_phase={phase_label(key): value for key, value in raw_phases.items()},
    )


def _table(title: str, rows: dict[str, Decimal]) -> list[str]:
    lines = [f"| {title} | US$ |", "|---|---:|"]
    ordered = sorted(rows.items(), key=lambda item: item[1], reverse=True)
    lines.extend(f"| {name} | {value:.4f} |" for name, value in ordered)
    if not rows:
        lines.append("| (sem dados) | 0.0000 |")
    return lines


def section_title(report: CostReport) -> str:
    return (
        f"## {report.run_date.isoformat()} ({report.start.isoformat()} a {report.end.isoformat()})"
    )


def render_section(report: CostReport) -> str:
    lines = [
        section_title(report),
        "",
        f"Custo bruto da conta: **US$ {report.account_total:.4f}**, "
        f"{report.budget_used_percent}% do orçamento de US$ {BUDGET_USD}. "
        "Créditos e reembolsos não estão incluídos.",
        "",
        *_table("Serviço (conta inteira)", report.by_service),
        "",
        *_table("Fase (tag Project=chuva-bh)", report.by_phase),
        "",
    ]
    return "\n".join(lines)


def upsert_section(path: Path, report: CostReport) -> None:
    """Grava a seção do relatório, substituindo a de mesma data e período."""
    current = path.read_text(encoding="utf-8") if path.exists() else LOG_HEADER
    section = render_section(report)
    pattern = re.compile(rf"^{re.escape(section_title(report))}\n.*?(?=^## |\Z)", re.M | re.S)
    if pattern.search(current):
        updated = pattern.sub(lambda _: f"{section}\n", current, count=1)
    else:
        updated = f"{current.rstrip()}\n\n{section}"
    path.write_text(f"{updated.rstrip()}\n", encoding="utf-8")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"data inválida: {value}") from error


def parse_args(argv: Sequence[str] | None, today: date) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Relatório de custo do projeto")
    parser.add_argument("--start", type=parse_date, default=PROJECT_START)
    parser.add_argument(
        "--end",
        type=parse_date,
        default=today + timedelta(days=1),
        help="data final, exclusiva",
    )
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--no-write", action="store_true", help="não altera o cost-log")
    parser.add_argument("--profile", help="perfil AWS a usar")
    args = parser.parse_args(argv)
    if args.start >= args.end:
        parser.error("--start deve ser anterior a --end")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    today = datetime.now(tz=UTC).date()
    args = parse_args(argv, today)
    try:
        session = boto3.Session(profile_name=args.profile, region_name="us-east-1")
        report = build_report(session.client("ce"), today, args.start, args.end)
    except (CostReportError, ClientError, BotoCoreError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1

    print(render_section(report))
    if not args.no_write:
        upsert_section(args.log_path, report)
        print(f"Registrado em {args.log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
