"""Canonical concept registry and SEC XBRL tag mappings.

Mappings live here so selection and query code never hard-code concept lists.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class StatementType(StrEnum):
    """Financial statement classification for a canonical concept."""

    INCOME = "income"
    BALANCE = "balance"
    CASH_FLOW = "cash_flow"
    DERIVED = "derived"


class PeriodType(StrEnum):
    """XBRL period type expected for a concept."""

    DURATION = "duration"
    INSTANT = "instant"
    EITHER = "either"


class SignMode(StrEnum):
    """How to normalize the reported numeric sign."""

    IDENTITY = "identity"
    ABS = "abs"
    NEGATE = "negate"


@dataclass(frozen=True, slots=True)
class CanonicalMapping:
    """Metadata describing how to resolve one canonical concept."""

    name: str
    statement: StatementType
    candidates: tuple[str, ...]
    expected_unit: str = "USD"
    period_type: PeriodType = PeriodType.DURATION
    allow_component_calculation: bool = False
    sign_mode: SignMode = SignMode.IDENTITY
    description: str = ""
    # Higher priority candidates appear earlier in ``candidates``.


@dataclass(frozen=True, slots=True)
class DerivedDefinition:
    """Transparent derived metric definition."""

    name: str
    statement: StatementType = StatementType.DERIVED
    required_inputs: tuple[str, ...] = ()
    optional_inputs: tuple[str, ...] = ()
    description: str = ""


def _m(
    name: str,
    statement: StatementType,
    candidates: list[str],
    *,
    unit: str = "USD",
    period_type: PeriodType = PeriodType.DURATION,
    allow_component_calculation: bool = False,
    sign_mode: SignMode = SignMode.IDENTITY,
    description: str = "",
) -> CanonicalMapping:
    return CanonicalMapping(
        name=name,
        statement=statement,
        candidates=tuple(candidates),
        expected_unit=unit,
        period_type=period_type,
        allow_component_calculation=allow_component_calculation,
        sign_mode=sign_mode,
        description=description,
    )


# Priority order within each list is highest → lowest.
CANONICAL_MAPPINGS: dict[str, CanonicalMapping] = {
    # --- Income statement ---
    "revenue": _m(
        "revenue",
        StatementType.INCOME,
        [
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
            "Revenues",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueGoodsNet",
        ],
        description="Total revenue / net sales",
    ),
    "cost_of_revenue": _m(
        "cost_of_revenue",
        StatementType.INCOME,
        [
            "CostOfGoodsAndServicesSold",
            "CostOfRevenue",
            "CostOfGoodsSold",
            "CostOfServices",
        ],
        description="Cost of revenue / COGS",
    ),
    "gross_profit": _m(
        "gross_profit",
        StatementType.INCOME,
        ["GrossProfit"],
        allow_component_calculation=True,
        description="Gross profit",
    ),
    "operating_income": _m(
        "operating_income",
        StatementType.INCOME,
        [
            "OperatingIncomeLoss",
        ],
        description="Operating income (loss)",
    ),
    "net_income": _m(
        "net_income",
        StatementType.INCOME,
        [
            "NetIncomeLoss",
            "ProfitLoss",
            "NetIncomeLossAvailableToCommonStockholdersBasic",
        ],
        description="Net income (loss)",
    ),
    "interest_expense": _m(
        "interest_expense",
        StatementType.INCOME,
        [
            "InterestExpense",
            "InterestExpenseDebt",
            "InterestAndDebtExpense",
        ],
        sign_mode=SignMode.ABS,
        description="Interest expense (normalized to positive magnitude)",
    ),
    "income_tax_expense": _m(
        "income_tax_expense",
        StatementType.INCOME,
        [
            "IncomeTaxExpenseBenefit",
        ],
        description="Income tax expense (benefit) from the income statement",
    ),
    "research_and_development": _m(
        "research_and_development",
        StatementType.INCOME,
        [
            "ResearchAndDevelopmentExpense",
            "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
        ],
        description="R&D expense",
    ),
    "selling_general_and_administrative": _m(
        "selling_general_and_administrative",
        StatementType.INCOME,
        [
            "SellingGeneralAndAdministrativeExpense",
            "GeneralAndAdministrativeExpense",
            "SellingAndMarketingExpense",
        ],
        description="SG&A expense",
    ),
    "diluted_eps": _m(
        "diluted_eps",
        StatementType.INCOME,
        [
            "EarningsPerShareDiluted",
            "EarningsPerShareBasicAndDiluted",
        ],
        unit="USD/shares",
        description="Diluted earnings per share",
    ),
    "diluted_shares": _m(
        "diluted_shares",
        StatementType.INCOME,
        [
            "WeightedAverageNumberOfDilutedSharesOutstanding",
            "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
        ],
        unit="shares",
        description="Diluted weighted-average shares",
    ),
    # --- Balance sheet (instant) ---
    "cash_and_equivalents": _m(
        "cash_and_equivalents",
        StatementType.BALANCE,
        [
            "CashAndCashEquivalentsAtCarryingValue",
            "Cash",
        ],
        period_type=PeriodType.INSTANT,
        description=(
            "Cash and cash equivalents only. Combined cash+STI tags are excluded "
            "to avoid double-counting when short_term_investments is also present."
        ),
    ),
    "short_term_investments": _m(
        "short_term_investments",
        StatementType.BALANCE,
        [
            "ShortTermInvestments",
            "MarketableSecuritiesCurrent",
            "AvailableForSaleSecuritiesCurrent",
        ],
        period_type=PeriodType.INSTANT,
    ),
    "total_current_assets": _m(
        "total_current_assets",
        StatementType.BALANCE,
        ["AssetsCurrent"],
        period_type=PeriodType.INSTANT,
    ),
    "total_assets": _m(
        "total_assets",
        StatementType.BALANCE,
        ["Assets"],
        period_type=PeriodType.INSTANT,
    ),
    "accounts_receivable": _m(
        "accounts_receivable",
        StatementType.BALANCE,
        [
            "AccountsReceivableNetCurrent",
            "ReceivablesNetCurrent",
            "AccountsReceivableNet",
        ],
        period_type=PeriodType.INSTANT,
    ),
    "inventory": _m(
        "inventory",
        StatementType.BALANCE,
        ["InventoryNet", "InventoryFinishedGoodsNetOfReserves"],
        period_type=PeriodType.INSTANT,
    ),
    "ppe": _m(
        "ppe",
        StatementType.BALANCE,
        [
            "PropertyPlantAndEquipmentNet",
            "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
        ],
        period_type=PeriodType.INSTANT,
    ),
    "goodwill": _m(
        "goodwill",
        StatementType.BALANCE,
        ["Goodwill"],
        period_type=PeriodType.INSTANT,
    ),
    "intangible_assets": _m(
        "intangible_assets",
        StatementType.BALANCE,
        [
            "IntangibleAssetsNetExcludingGoodwill",
            "FiniteLivedIntangibleAssetsNet",
        ],
        period_type=PeriodType.INSTANT,
    ),
    "total_current_liabilities": _m(
        "total_current_liabilities",
        StatementType.BALANCE,
        ["LiabilitiesCurrent"],
        period_type=PeriodType.INSTANT,
    ),
    "total_liabilities": _m(
        "total_liabilities",
        StatementType.BALANCE,
        ["Liabilities"],
        period_type=PeriodType.INSTANT,
        allow_component_calculation=True,
    ),
    "short_term_debt": _m(
        "short_term_debt",
        StatementType.BALANCE,
        [
            "DebtCurrent",
            "ShortTermBorrowings",
            "CommercialPaper",
            "LongTermDebtCurrent",
        ],
        period_type=PeriodType.INSTANT,
        allow_component_calculation=True,
        description=(
            "Prefer DebtCurrent when present; otherwise sum non-overlapping "
            "ShortTermBorrowings/CommercialPaper + LongTermDebtCurrent."
        ),
    ),
    "long_term_debt": _m(
        "long_term_debt",
        StatementType.BALANCE,
        [
            "LongTermDebtNoncurrent",
            "LongTermDebtAndCapitalLeaseObligations",
            "LongTermDebt",
        ],
        period_type=PeriodType.INSTANT,
        description="Prefer noncurrent LTD; LongTermDebt may include current maturities",
    ),
    "total_debt": _m(
        "total_debt",
        StatementType.BALANCE,
        [
            "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
            "DebtAndCapitalLeaseObligations",
        ],
        period_type=PeriodType.INSTANT,
        allow_component_calculation=True,
        description="Direct total-debt tag preferred; else short_term_debt + long_term_debt",
    ),
    "stockholders_equity": _m(
        "stockholders_equity",
        StatementType.BALANCE,
        [
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ],
        period_type=PeriodType.INSTANT,
    ),
    "retained_earnings": _m(
        "retained_earnings",
        StatementType.BALANCE,
        [
            "RetainedEarningsAccumulatedDeficit",
            "RetainedEarningsAccumulatedDeficitAndOtherComprehensiveIncomeLoss",
        ],
        period_type=PeriodType.INSTANT,
    ),
    # --- Cash flow ---
    "operating_cash_flow": _m(
        "operating_cash_flow",
        StatementType.CASH_FLOW,
        [
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ],
    ),
    "capital_expenditures": _m(
        "capital_expenditures",
        StatementType.CASH_FLOW,
        [
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
        ],
        sign_mode=SignMode.ABS,
        description=(
            "Cash CapEx outflows. Mapping uses ABS because SEC Payment* tags "
            "are often negative cash outflows; normalized value is a positive "
            "spend amount for FCF = OCF - CapEx. Reported raw value is retained "
            "in provenance.reported_value."
        ),
    ),
    "investing_cash_flow": _m(
        "investing_cash_flow",
        StatementType.CASH_FLOW,
        [
            "NetCashProvidedByUsedInInvestingActivities",
            "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
        ],
    ),
    "financing_cash_flow": _m(
        "financing_cash_flow",
        StatementType.CASH_FLOW,
        [
            "NetCashProvidedByUsedInFinancingActivities",
            "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
        ],
    ),
    "depreciation_and_amortization": _m(
        "depreciation_and_amortization",
        StatementType.CASH_FLOW,
        [
            "DepreciationDepletionAndAmortization",
            "DepreciationAndAmortization",
            "Depreciation",
        ],
        sign_mode=SignMode.ABS,
    ),
    "stock_based_compensation": _m(
        "stock_based_compensation",
        StatementType.CASH_FLOW,
        [
            "ShareBasedCompensation",
            "AllocatedShareBasedCompensationExpense",
        ],
        sign_mode=SignMode.ABS,
    ),
    "dividends_paid": _m(
        "dividends_paid",
        StatementType.CASH_FLOW,
        [
            "PaymentsOfDividends",
            "PaymentsOfDividendsCommonStock",
            "PaymentsOfOrdinaryDividends",
        ],
        sign_mode=SignMode.ABS,
    ),
    "share_repurchases": _m(
        "share_repurchases",
        StatementType.CASH_FLOW,
        [
            "PaymentsForRepurchaseOfCommonStock",
            "PaymentsForRepurchaseOfEquity",
        ],
        sign_mode=SignMode.ABS,
    ),
    "debt_issuance": _m(
        "debt_issuance",
        StatementType.CASH_FLOW,
        [
            "ProceedsFromIssuanceOfLongTermDebt",
            "ProceedsFromDebtNetOfIssuanceCosts",
            "ProceedsFromIssuanceOfDebt",
        ],
        sign_mode=SignMode.ABS,
    ),
    "debt_repayment": _m(
        "debt_repayment",
        StatementType.CASH_FLOW,
        [
            "RepaymentsOfLongTermDebt",
            "RepaymentsOfDebt",
            "RepaymentsOfLongTermDebtAndCapitalSecurities",
        ],
        sign_mode=SignMode.ABS,
    ),
}


DERIVED_DEFINITIONS: dict[str, DerivedDefinition] = {
    "free_cash_flow": DerivedDefinition(
        name="free_cash_flow",
        required_inputs=("operating_cash_flow", "capital_expenditures"),
        description="operating_cash_flow - capital_expenditures",
    ),
    "net_debt": DerivedDefinition(
        name="net_debt",
        required_inputs=("total_debt", "cash_and_equivalents"),
        optional_inputs=("short_term_investments",),
        description="total_debt - cash - short_term_investments",
    ),
    "working_capital": DerivedDefinition(
        name="working_capital",
        required_inputs=("total_current_assets", "total_current_liabilities"),
        description="total_current_assets - total_current_liabilities",
    ),
    "invested_capital": DerivedDefinition(
        name="invested_capital",
        required_inputs=("total_debt", "stockholders_equity"),
        optional_inputs=("cash_and_equivalents", "short_term_investments"),
        description="total_debt + equity - cash - STI (when available)",
    ),
    "ebitda": DerivedDefinition(
        name="ebitda",
        required_inputs=("operating_income", "depreciation_and_amortization"),
        description="operating_income + D&A (transparent approximation)",
    ),
    "nopat": DerivedDefinition(
        name="nopat",
        required_inputs=("operating_income", "income_tax_expense"),
        description="operating_income * (1 - tax_rate) when tax rate can be inferred",
    ),
}


INCOME_CONCEPTS: tuple[str, ...] = tuple(
    name for name, m in CANONICAL_MAPPINGS.items() if m.statement == StatementType.INCOME
)
BALANCE_CONCEPTS: tuple[str, ...] = tuple(
    name for name, m in CANONICAL_MAPPINGS.items() if m.statement == StatementType.BALANCE
)
CASH_FLOW_CONCEPTS: tuple[str, ...] = tuple(
    name for name, m in CANONICAL_MAPPINGS.items() if m.statement == StatementType.CASH_FLOW
)

# Candidate concept -> (canonical_name, priority_index)
CANDIDATE_INDEX: dict[str, tuple[str, int]] = {
    candidate: (mapping.name, idx)
    for mapping in CANONICAL_MAPPINGS.values()
    for idx, candidate in enumerate(mapping.candidates)
}


def get_mapping(name: str) -> CanonicalMapping:
    """Return a mapping by canonical name or raise KeyError."""
    return CANONICAL_MAPPINGS[name]


def all_candidate_concepts() -> frozenset[str]:
    """Return every XBRL concept referenced by the registry."""
    return frozenset(CANDIDATE_INDEX)


def apply_sign(value: float, mode: SignMode) -> float:
    """Apply sign normalization."""
    if mode is SignMode.IDENTITY:
        return value
    if mode is SignMode.ABS:
        return abs(value)
    if mode is SignMode.NEGATE:
        return -value
    raise ValueError(f"Unknown sign mode: {mode!r}")


# Keep Literal export available for typed call sites.
PeriodKind = Literal["annual", "quarterly"]

__all__ = [
    "BALANCE_CONCEPTS",
    "CANDIDATE_INDEX",
    "CANONICAL_MAPPINGS",
    "CASH_FLOW_CONCEPTS",
    "DERIVED_DEFINITIONS",
    "INCOME_CONCEPTS",
    "CanonicalMapping",
    "DerivedDefinition",
    "PeriodType",
    "SignMode",
    "StatementType",
    "all_candidate_concepts",
    "apply_sign",
    "get_mapping",
]
