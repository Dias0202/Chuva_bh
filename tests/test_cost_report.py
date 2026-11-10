"""Testa o relatório de custo, com o Cost Explorer simulado pelo moto."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import boto3
import cost_report
import pytest
import requests
from botocore.exceptions import ClientError
from cost_report import CostReport, CostReportError
from moto import mock_aws

START = date(2026, 11, 1)
END = date(2026, 12, 1)
RUN_DATE = date(2026, 11, 20)


def group(key: str, amount: str) -> dict[str, Any]:
    return {"Keys": [key], "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": "USD"}}}


def response(*groups: dict[str, Any]) -> dict[str, Any]:
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": START.isoformat(), "End": END.isoformat()},
                "Total": {},
                "Groups": list(groups),
                "Estimated": True,
            }
        ],
        "DimensionValueAttributes": [],
    }


def queue_results(*results: dict[str, Any]) -> None:
    posted = requests.post(
        "http://motoapi.amazonaws.com/moto-api/static/ce/cost-and-usage-results",
        json={"results": list(results)},
        timeout=5,
    )
    posted.raise_for_status()


@pytest.fixture
def ce() -> Iterator[Any]:
    with mock_aws():
        yield boto3.client("ce", region_name="us-east-1")


def make_report(
    by_service: dict[str, Decimal] | None = None, run_date: date = RUN_DATE
) -> CostReport:
    return CostReport(
        run_date=run_date,
        start=START,
        end=END,
        by_service=by_service if by_service is not None else {"Amazon S3": Decimal("1.5")},
        by_phase={"Fase 0": Decimal("1.5")},
    )


def test_gross_filter_excludes_credits_and_refunds() -> None:
    assert cost_report.gross_cost_filter() == {
        "Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}
    }


def test_project_filter_combines_tag_with_credit_exclusion() -> None:
    expression = cost_report.project_filter()

    assert expression["And"][0] == cost_report.gross_cost_filter()
    assert expression["And"][1] == {"Tags": {"Key": "Project", "Values": ["chuva-bh"]}}


def test_build_report_aggregates_service_and_phase(ce: Any) -> None:
    queue_results(
        response(group("Amazon S3", "1.25"), group("AWS Key Management Service", "1.00")),
        response(group("Phase$0", "2.00"), group("Phase$", "0.25")),
    )

    report = cost_report.build_report(ce, RUN_DATE, START, END)

    assert report.by_service == {
        "Amazon S3": Decimal("1.25"),
        "AWS Key Management Service": Decimal("1.00"),
    }
    assert report.by_phase == {
        "Fase 0": Decimal("2.00"),
        cost_report.UNTAGGED_LABEL: Decimal("0.25"),
    }
    assert report.account_total == Decimal("2.25")


def test_fetch_costs_sums_multiple_periods_and_pages() -> None:
    ce = MagicMock()
    first = response(group("Amazon S3", "1.10"))
    first["NextPageToken"] = "proxima"
    ce.get_cost_and_usage.side_effect = [first, response(group("Amazon S3", "0.20"))]

    totals = cost_report.fetch_costs(
        ce, START, END, "DIMENSION", "SERVICE", cost_report.gross_cost_filter()
    )

    assert totals == {"Amazon S3": Decimal("1.30")}
    assert ce.get_cost_and_usage.call_args_list[1].kwargs["NextPageToken"] == "proxima"


def test_fetch_costs_stops_at_the_page_limit_to_bound_api_cost() -> None:
    ce = MagicMock()
    endless = response(group("Amazon S3", "1"))
    endless["NextPageToken"] = "sempre"
    ce.get_cost_and_usage.return_value = endless

    with pytest.raises(CostReportError, match="páginas"):
        cost_report.fetch_costs(
            ce, START, END, "DIMENSION", "SERVICE", cost_report.gross_cost_filter()
        )

    assert ce.get_cost_and_usage.call_count == cost_report.MAX_PAGES


def test_budget_percentage_uses_the_project_limit() -> None:
    assert make_report({"Amazon S3": Decimal("15")}).budget_used_percent == Decimal("25.0")


def test_render_section_states_gross_values_and_handles_no_data() -> None:
    rendered = cost_report.render_section(make_report({}))

    assert "Créditos e reembolsos não estão incluídos" in rendered
    assert "(sem dados)" in rendered


def test_upsert_creates_the_log_when_missing(tmp_path: Path) -> None:
    path = tmp_path / "cost-log.md"

    cost_report.upsert_section(path, make_report())

    content = path.read_text(encoding="utf-8")
    assert content.startswith("# Registro de custos")
    assert "Amazon S3" in content


def test_upsert_replaces_the_same_day_and_period(tmp_path: Path) -> None:
    path = tmp_path / "cost-log.md"
    cost_report.upsert_section(path, make_report({"Amazon S3": Decimal("1")}))

    cost_report.upsert_section(path, make_report({"Amazon S3": Decimal("2")}))

    content = path.read_text(encoding="utf-8")
    assert content.count("## 2026-11-20") == 1
    assert "2.0000" in content
    assert "1.0000" not in content


def test_upsert_keeps_other_entries(tmp_path: Path) -> None:
    path = tmp_path / "cost-log.md"
    cost_report.upsert_section(path, make_report(run_date=date(2026, 11, 19)))
    cost_report.upsert_section(path, make_report(run_date=date(2026, 11, 20)))

    cost_report.upsert_section(
        path, make_report({"Amazon S3": Decimal("9")}, run_date=date(2026, 11, 19))
    )

    content = path.read_text(encoding="utf-8")
    assert content.count("## 2026-11-19") == 1
    assert content.count("## 2026-11-20") == 1
    assert content.index("## 2026-11-19") < content.index("## 2026-11-20")
    assert "9.0000" in content


def test_main_writes_log(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "cost-log.md"
    with mock_aws():
        queue_results(response(group("Amazon S3", "1.00")), response(group("Phase$0", "1.00")))

        code = cost_report.main(
            ["--start", "2026-11-01", "--end", "2026-12-01", "--log-path", str(path)]
        )

    assert code == 0
    assert "Amazon S3" in path.read_text(encoding="utf-8")
    assert "Amazon S3" in capsys.readouterr().out


def test_main_no_write_leaves_the_log_untouched(tmp_path: Path) -> None:
    path = tmp_path / "cost-log.md"
    with mock_aws():
        queue_results(response(), response())

        assert cost_report.main(["--no-write", "--log-path", str(path)]) == 0

    assert not path.exists()


def test_main_reports_api_errors_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = MagicMock()
    client.get_cost_and_usage.side_effect = ClientError(
        {"Error": {"Code": "DataUnavailableException", "Message": "indisponível"}},
        "GetCostAndUsage",
    )
    session = MagicMock()
    session.client.return_value = client
    monkeypatch.setattr(boto3, "Session", MagicMock(return_value=session))

    assert cost_report.main(["--no-write"]) == 1
    assert "DataUnavailableException" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--start", "2026-12-01", "--end", "2026-11-01"],
        ["--start", "2026-11-01", "--end", "2026-11-01"],
        ["--start", "ontem"],
    ],
)
def test_main_rejects_invalid_periods(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cost_report.main(argv)
    assert raised.value.code == 2
