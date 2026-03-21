from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg


@dataclass(frozen=True)
class CompanyProfile:
    company_id: str
    name: str
    industry: str
    naics: str
    jurisdiction: str
    legal_type: str
    founded_year: int
    employee_count: int
    risk_segment: str
    trajectory: str
    submission_channel: str
    ip_region: str


@dataclass(frozen=True)
class FinancialYear:
    fiscal_year: int
    total_revenue: Decimal
    gross_profit: Decimal
    operating_expenses: Decimal
    operating_income: Decimal
    ebitda: Decimal
    depreciation_amortization: Decimal
    interest_expense: Decimal
    income_before_tax: Decimal
    tax_expense: Decimal
    net_income: Decimal
    total_assets: Decimal
    current_assets: Decimal
    cash_and_equivalents: Decimal
    accounts_receivable: Decimal
    inventory: Decimal
    total_liabilities: Decimal
    current_liabilities: Decimal
    long_term_debt: Decimal
    total_equity: Decimal
    operating_cash_flow: Decimal
    investing_cash_flow: Decimal
    financing_cash_flow: Decimal
    free_cash_flow: Decimal
    debt_to_equity: Decimal | None
    current_ratio: Decimal | None
    debt_to_ebitda: Decimal | None
    interest_coverage_ratio: Decimal | None
    gross_margin: Decimal | None
    ebitda_margin: Decimal | None
    net_margin: Decimal | None
    balance_sheet_check: bool


@dataclass(frozen=True)
class ComplianceFlag:
    flag_type: str
    severity: str
    is_active: bool
    added_date: date
    note: str


class ApplicantRegistryClient:
    """Read-only access to the external Applicant Registry schema."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_company(self, company_id: str) -> CompanyProfile | None:
        row = await self._pool.fetchrow(
            """
            SELECT
                company_id, name, industry, naics, jurisdiction, legal_type,
                founded_year, employee_count, risk_segment, trajectory,
                submission_channel, ip_region
            FROM applicant_registry.companies
            WHERE company_id = $1
            """,
            company_id,
        )
        if row is None:
            return None
        return CompanyProfile(
            company_id=row["company_id"],
            name=row["name"],
            industry=row["industry"],
            naics=row["naics"],
            jurisdiction=row["jurisdiction"],
            legal_type=row["legal_type"],
            founded_year=row["founded_year"],
            employee_count=row["employee_count"],
            risk_segment=row["risk_segment"],
            trajectory=row["trajectory"],
            submission_channel=row["submission_channel"],
            ip_region=row["ip_region"],
        )

    async def get_financial_history(
        self, company_id: str, years: list[int] | None = None
    ) -> list[FinancialYear]:
        if years:
            rows = await self._pool.fetch(
                """
                SELECT
                    fiscal_year, total_revenue, gross_profit, operating_expenses,
                    operating_income, ebitda, depreciation_amortization,
                    interest_expense, income_before_tax, tax_expense, net_income,
                    total_assets, current_assets, cash_and_equivalents,
                    accounts_receivable, inventory, total_liabilities,
                    current_liabilities, long_term_debt, total_equity,
                    operating_cash_flow, investing_cash_flow, financing_cash_flow,
                    free_cash_flow, debt_to_equity, current_ratio, debt_to_ebitda,
                    interest_coverage_ratio, gross_margin, ebitda_margin, net_margin,
                    balance_sheet_check
                FROM applicant_registry.financial_history
                WHERE company_id = $1
                  AND fiscal_year = ANY($2::int[])
                ORDER BY fiscal_year ASC
                """,
                company_id,
                years,
            )
        else:
            rows = await self._pool.fetch(
                """
                SELECT
                    fiscal_year, total_revenue, gross_profit, operating_expenses,
                    operating_income, ebitda, depreciation_amortization,
                    interest_expense, income_before_tax, tax_expense, net_income,
                    total_assets, current_assets, cash_and_equivalents,
                    accounts_receivable, inventory, total_liabilities,
                    current_liabilities, long_term_debt, total_equity,
                    operating_cash_flow, investing_cash_flow, financing_cash_flow,
                    free_cash_flow, debt_to_equity, current_ratio, debt_to_ebitda,
                    interest_coverage_ratio, gross_margin, ebitda_margin, net_margin,
                    balance_sheet_check
                FROM applicant_registry.financial_history
                WHERE company_id = $1
                ORDER BY fiscal_year ASC
                """,
                company_id,
            )

        return [
            FinancialYear(
                fiscal_year=r["fiscal_year"],
                total_revenue=r["total_revenue"],
                gross_profit=r["gross_profit"],
                operating_expenses=r["operating_expenses"],
                operating_income=r["operating_income"],
                ebitda=r["ebitda"],
                depreciation_amortization=r["depreciation_amortization"],
                interest_expense=r["interest_expense"],
                income_before_tax=r["income_before_tax"],
                tax_expense=r["tax_expense"],
                net_income=r["net_income"],
                total_assets=r["total_assets"],
                current_assets=r["current_assets"],
                cash_and_equivalents=r["cash_and_equivalents"],
                accounts_receivable=r["accounts_receivable"],
                inventory=r["inventory"],
                total_liabilities=r["total_liabilities"],
                current_liabilities=r["current_liabilities"],
                long_term_debt=r["long_term_debt"],
                total_equity=r["total_equity"],
                operating_cash_flow=r["operating_cash_flow"],
                investing_cash_flow=r["investing_cash_flow"],
                financing_cash_flow=r["financing_cash_flow"],
                free_cash_flow=r["free_cash_flow"],
                debt_to_equity=r.get("debt_to_equity"),
                current_ratio=r.get("current_ratio"),
                debt_to_ebitda=r.get("debt_to_ebitda"),
                interest_coverage_ratio=r.get("interest_coverage_ratio"),
                gross_margin=r.get("gross_margin"),
                ebitda_margin=r.get("ebitda_margin"),
                net_margin=r.get("net_margin"),
                balance_sheet_check=r["balance_sheet_check"],
            )
            for r in rows
        ]

    async def get_compliance_flags(
        self, company_id: str, active_only: bool = False
    ) -> list[ComplianceFlag]:
        if active_only:
            rows = await self._pool.fetch(
                """
                SELECT flag_type, severity, is_active, added_date, note
                FROM applicant_registry.compliance_flags
                WHERE company_id = $1 AND is_active = TRUE
                ORDER BY added_date DESC
                """,
                company_id,
            )
        else:
            rows = await self._pool.fetch(
                """
                SELECT flag_type, severity, is_active, added_date, note
                FROM applicant_registry.compliance_flags
                WHERE company_id = $1
                ORDER BY added_date DESC
                """,
                company_id,
            )

        return [
            ComplianceFlag(
                flag_type=r["flag_type"],
                severity=r["severity"],
                is_active=r["is_active"],
                added_date=r["added_date"],
                note=r["note"],
            )
            for r in rows
        ]

    async def get_loan_relationships(self, company_id: str) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            """
            SELECT
                loan_amount,
                loan_year,
                was_repaid,
                default_occurred,
                note
            FROM applicant_registry.loan_relationships
            WHERE company_id = $1
            ORDER BY loan_year ASC
            """,
            company_id,
        )
        return [dict(r) for r in rows]

