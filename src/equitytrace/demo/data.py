"""Deterministic, clearly synthetic research dataset for the offline demo.

Everything here is fictional. Issuers use CIKs in a range the SEC has never
assigned, accession numbers are generated, and every fact carries a
``synthetic://`` source URL. Prices come from a fixed-seed integer PRNG followed
by basic IEEE arithmetic and cent rounding, so the content is identical on every
platform and under every ``PYTHONHASHSEED``.

Prices are generated independently of the fundamentals. The dataset makes no
claim that any factor predicts returns; it exists to exercise the real engine.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from equitytrace.market.availability import bar_available_at
from equitytrace.market.models import (
    AssetType,
    DailyPriceBar,
    MarketDataProviderName,
    PriceAdjustmentMode,
    make_instrument_id,
)
from equitytrace.models import Filing, FinancialFact, Issuer, Security

DATASET_ID = "equitytrace-synthetic-demo"
DATASET_VERSION = "1"
SYNTHETIC_NOTICE = (
    "SYNTHETIC DEMO DATA — fictional issuers, filings and prices. Nothing here was "
    "downloaded from the SEC or a market-data vendor, none of it is historical "
    "investment performance, and none of it is investment advice."
)

CALENDAR_SYMBOL = "SYNX"
BENCHMARK_SYMBOL = "SYNX"
BENCHMARK_NAME = "SYNX — Synthetic Broad Market Index (fictional SPY-like benchmark)"

PRICE_START = date(2022, 7, 1)
PRICE_END = date(2024, 12, 31)
FETCHED_AT = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)
EASTERN = ZoneInfo("America/New_York")
_FILING_ACCEPTANCE_LOCAL = time(16, 30)

# NYSE full-day closures inside the price window (weekday holidays only).
_MARKET_HOLIDAYS = frozenset(
    date.fromisoformat(item)
    for item in (
        "2022-09-05",
        "2022-11-24",
        "2022-12-26",
        "2023-01-02",
        "2023-01-16",
        "2023-02-20",
        "2023-04-07",
        "2023-05-29",
        "2023-06-19",
        "2023-07-04",
        "2023-09-04",
        "2023-11-23",
        "2023-12-25",
        "2024-01-01",
        "2024-01-15",
        "2024-02-19",
        "2024-03-29",
        "2024-05-27",
        "2024-06-19",
        "2024-07-04",
        "2024-09-02",
        "2024-11-28",
        "2024-12-25",
    )
)


@dataclass(frozen=True, slots=True)
class FiscalYear:
    """One synthetic fiscal year in USD millions (balance values at year end)."""

    revenue: float
    operating_income: float
    net_income: float
    total_assets: float
    operating_cash_flow: float
    capital_expenditures: float
    long_term_debt: float
    current_debt: float


@dataclass(frozen=True, slots=True)
class CompanySpec:
    ticker: str
    cik: str
    legal_name: str
    sector: str
    story: str
    # (month, day) the 10-K is accepted after the close; weekends roll to Monday.
    filing_month_day: tuple[int, int]
    years: dict[int, FiscalYear]
    annual_drift: float
    beta: float
    idio_vol: float
    start_price: float
    extra_tickers: tuple[str, ...] = ()


def _fy(*values: float) -> FiscalYear:
    return FiscalYear(*values)


COMPANIES: tuple[CompanySpec, ...] = (
    CompanySpec(
        ticker="SYAL",
        cik="0990000001",
        legal_name="Alder Systems (Synthetic) Inc",
        sector="Software",
        story="High growth and expanding margins.",
        filing_month_day=(2, 8),
        years={
            2021: _fy(800, 160, 128, 1100, 190, 40, 200, 20),
            2022: _fy(1000, 230, 185, 1350, 270, 55, 180, 20),
            2023: _fy(1280, 320, 262, 1700, 350, 70, 150, 20),
        },
        annual_drift=0.0,
        beta=1.25,
        idio_vol=0.014,
        start_price=88.0,
    ),
    CompanySpec(
        ticker="SYBR",
        cik="0990000002",
        legal_name="Birch Retail Group (Synthetic) Inc",
        sector="Retail",
        story="Slow growth and thin, slightly eroding margins.",
        filing_month_day=(3, 14),
        years={
            2021: _fy(3000, 150, 90, 1800, 160, 110, 500, 60),
            2022: _fy(3150, 142, 82, 1850, 150, 105, 520, 60),
            2023: _fy(3250, 146, 85, 1900, 165, 100, 540, 60),
        },
        annual_drift=0.0,
        beta=0.70,
        idio_vol=0.010,
        start_price=61.0,
    ),
    CompanySpec(
        ticker="SYCD",
        cik="0990000003",
        legal_name="Cedar Devices (Synthetic) Corp",
        sector="Hardware",
        story="Cyclical: a strong FY2022 followed by a contraction.",
        filing_month_day=(2, 22),
        years={
            2021: _fy(1500, 180, 140, 1400, 200, 90, 300, 30),
            2022: _fy(1850, 260, 205, 1650, 280, 120, 260, 30),
            2023: _fy(1700, 190, 150, 1620, 210, 85, 280, 30),
        },
        annual_drift=0.0,
        beta=1.10,
        idio_vol=0.016,
        start_price=112.0,
    ),
    CompanySpec(
        ticker="SYDL",
        cik="0990000004",
        legal_name="Delta Logistics (Synthetic) Co",
        sector="Transportation",
        story="Steady growth with heavy capital spending.",
        filing_month_day=(2, 28),
        years={
            2021: _fy(2200, 220, 150, 2600, 300, 220, 900, 70),
            2022: _fy(2420, 254, 175, 2800, 340, 250, 950, 70),
            2023: _fy(2640, 290, 205, 3050, 380, 270, 1000, 70),
        },
        annual_drift=0.0,
        beta=0.90,
        idio_vol=0.011,
        start_price=74.0,
    ),
    CompanySpec(
        ticker="SYEN",
        cik="0990000005",
        legal_name="Elm Energy Partners (Synthetic) Ltd",
        sector="Energy",
        story="Commodity-driven spike in FY2022, partial reversal in FY2023.",
        filing_month_day=(2, 15),
        years={
            2021: _fy(1800, 200, 120, 3200, 420, 380, 1100, 90),
            2022: _fy(2900, 520, 390, 3500, 810, 450, 1000, 90),
            2023: _fy(2300, 330, 240, 3400, 560, 480, 950, 90),
        },
        annual_drift=0.0,
        beta=0.95,
        idio_vol=0.018,
        start_price=57.0,
    ),
    CompanySpec(
        ticker="SYFM",
        cik="0990000006",
        legal_name="Fir Medical (Synthetic) Holdings",
        sector="Healthcare",
        story="Steady growth with improving margins and low capital intensity.",
        filing_month_day=(3, 7),
        years={
            2021: _fy(950, 142, 105, 1500, 150, 45, 250, 25),
            2022: _fy(1040, 166, 125, 1580, 175, 48, 230, 25),
            2023: _fy(1150, 196, 150, 1700, 205, 50, 210, 25),
        },
        annual_drift=0.0,
        beta=0.60,
        idio_vol=0.009,
        start_price=95.0,
    ),
    CompanySpec(
        ticker="SYGA",
        cik="0990000007",
        legal_name="Grove Materials (Synthetic) Corp",
        sector="Materials",
        story="Mature, capital-heavy, declining FY2023 profit. Two share classes.",
        filing_month_day=(2, 26),
        years={
            2021: _fy(1300, 130, 85, 2000, 170, 140, 700, 40),
            2022: _fy(1380, 150, 98, 2050, 185, 150, 690, 40),
            2023: _fy(1330, 120, 70, 2100, 150, 160, 720, 40),
        },
        annual_drift=0.0,
        beta=0.85,
        idio_vol=0.012,
        start_price=48.0,
        extra_tickers=("SYGB",),
    ),
)

# Share class B tracks the same issuer; it exists to exercise identity protection.
_CLASS_B = {"SYGB": (0.0, 0.85, 0.013, 46.5)}

_FISCAL_YEARS = (2021, 2022, 2023)
_MARKET_DRIFT = 0.06
_MARKET_VOL = 0.008
_SHOCKS: dict[tuple[str, date], float] = {("SYEN", date(2024, 4, 17)): -0.14}


@dataclass(frozen=True, slots=True)
class CompanyBundle:
    """Everything stored for one synthetic issuer."""

    spec: CompanySpec
    issuer: Issuer
    securities: tuple[Security, ...]
    filings: tuple[Filing, ...]
    facts: tuple[FinancialFact, ...]


@dataclass(frozen=True, slots=True)
class SeriesBundle:
    """One instrument and its adjusted-close style daily bars."""

    symbol: str
    name: str
    asset_type: AssetType
    issuer_cik: str | None
    bars: tuple[DailyPriceBar, ...]


@dataclass(frozen=True, slots=True)
class DemoDataset:
    companies: tuple[CompanyBundle, ...]
    series: tuple[SeriesBundle, ...]

    @property
    def tickers(self) -> tuple[str, ...]:
        return tuple(sorted(s.ticker for c in self.companies for s in c.securities))

    @property
    def fact_count(self) -> int:
        return sum(len(c.facts) for c in self.companies)

    @property
    def bar_count(self) -> int:
        return sum(len(s.bars) for s in self.series)

    def digest(self) -> str:
        return dataset_digest(self)


class _SplitMix64:
    """Tiny integer PRNG so the dataset never depends on library floating point."""

    def __init__(self, seed: int) -> None:
        self._state = seed & 0xFFFFFFFFFFFFFFFF

    def next_u64(self) -> int:
        self._state = (self._state + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        z = self._state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
        return z ^ (z >> 31)

    def uniform(self) -> float:
        return (self.next_u64() >> 11) / float(1 << 53)

    def normal(self) -> float:
        """Irwin-Hall approximation of N(0, 1): sum of 12 uniforms minus 6."""
        return sum(self.uniform() for _ in range(12)) - 6.0


def _seed(symbol: str) -> int:
    digest = hashlib.sha256(f"{DATASET_ID}:{DATASET_VERSION}:{symbol}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def trading_sessions() -> tuple[date, ...]:
    """Synthetic exchange calendar: weekdays minus listed NYSE closures."""
    days: list[date] = []
    day = PRICE_START
    while day <= PRICE_END:
        if day.weekday() < 5 and day not in _MARKET_HOLIDAYS:
            days.append(day)
        day += timedelta(days=1)
    return tuple(days)


def _filing_date(year: int, month_day: tuple[int, int]) -> date:
    day = date(year, *month_day)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _accession(cik: str, fiscal_year: int) -> str:
    return f"{cik}-{(fiscal_year + 1) % 100:02d}-{1:06d}"


def _share_class(index: int, multi_class: bool) -> str:
    if not multi_class:
        return "Common"
    return "Class A" if index == 0 else "Class B"


def _build_company(spec: CompanySpec) -> CompanyBundle:
    issuer = Issuer(
        cik=spec.cik,
        legal_name=spec.legal_name,
        entity_type="operating",
        sic_description=f"SYNTHETIC {spec.sector}",
        fiscal_year_end="1231",
    )
    tickers = (spec.ticker, *spec.extra_tickers)
    securities = tuple(
        Security(
            ticker=ticker,
            cik=spec.cik,
            title=f"{spec.legal_name} — {_share_class(i, bool(spec.extra_tickers))}",
            exchange="SYNTHETIC",
            is_primary=i == 0,
        )
        for i, ticker in enumerate(tickers)
    )
    filings: list[Filing] = []
    facts: list[FinancialFact] = []
    for fiscal_year in _FISCAL_YEARS:
        filed = _filing_date(fiscal_year + 1, spec.filing_month_day)
        accepted = datetime.combine(filed, _FILING_ACCEPTANCE_LOCAL, tzinfo=EASTERN).astimezone(UTC)
        accession = _accession(spec.cik, fiscal_year)
        url = f"synthetic://equitytrace-demo/v{DATASET_VERSION}/{spec.ticker}/{accession}"
        period_start = date(fiscal_year, 1, 1)
        period_end = date(fiscal_year, 12, 31)
        filings.append(
            Filing(
                accession_number=accession,
                cik=spec.cik,
                form="10-K",
                filing_date=filed,
                report_date=period_end,
                acceptance_datetime=accepted,
                available_at=accepted,
                primary_document=f"{spec.ticker.lower()}-{fiscal_year}-synthetic-10k.htm",
                is_xbrl=True,
                is_inline_xbrl=True,
                source_url=url,
            )
        )
        year = spec.years[fiscal_year]
        rows: tuple[tuple[str, float, bool], ...] = (
            ("RevenueFromContractWithCustomerExcludingAssessedTax", year.revenue, False),
            ("OperatingIncomeLoss", year.operating_income, False),
            ("NetIncomeLoss", year.net_income, False),
            ("Assets", year.total_assets, True),
            ("NetCashProvidedByUsedInOperatingActivities", year.operating_cash_flow, False),
            ("PaymentsToAcquirePropertyPlantAndEquipment", year.capital_expenditures, False),
            ("LongTermDebtNoncurrent", year.long_term_debt, True),
            ("DebtCurrent", year.current_debt, True),
        )
        for concept, millions, instant in rows:
            facts.append(
                FinancialFact(
                    cik=spec.cik,
                    taxonomy="us-gaap",
                    concept=concept,
                    label=f"SYNTHETIC {concept}",
                    description=(
                        f"Synthetic {spec.ticker} FY{fiscal_year} value generated for the "
                        "EquityTrace demo; not an SEC filing."
                    ),
                    unit="USD",
                    value=millions * 1_000_000.0,
                    start_date=None if instant else period_start,
                    end_date=period_end,
                    filing_date=filed,
                    acceptance_datetime=accepted,
                    available_at=accepted,
                    accession_number=accession,
                    form="10-K",
                    fiscal_year=fiscal_year,
                    fiscal_period="FY",
                    frame=None,
                    source_url=url,
                ).with_fact_id()
            )
    return CompanyBundle(
        spec=spec,
        issuer=issuer,
        securities=securities,
        filings=tuple(filings),
        facts=tuple(facts),
    )


def _price_path(
    symbol: str,
    sessions: tuple[date, ...],
    market: list[float],
    *,
    drift: float,
    beta: float,
    idio_vol: float,
    start_price: float,
) -> list[float]:
    rng = _SplitMix64(_seed(symbol))
    daily_drift = drift / 252.0
    price = start_price
    closes: list[float] = []
    for index, day in enumerate(sessions):
        if index > 0:
            ret = daily_drift + beta * market[index] + idio_vol * rng.normal()
            ret += _SHOCKS.get((symbol, day), 0.0)
            price = max(1.0, round(price * (1.0 + ret), 2))
        closes.append(price)
    return closes


def _market_returns(sessions: tuple[date, ...]) -> list[float]:
    rng = _SplitMix64(_seed("MARKET"))
    daily_drift = _MARKET_DRIFT / 252.0
    return [
        0.0 if i == 0 else daily_drift + _MARKET_VOL * rng.normal() for i in range(len(sessions))
    ]


def _to_bars(
    closes: list[float],
    sessions: tuple[date, ...],
    *,
    symbol: str,
) -> tuple[DailyPriceBar, ...]:
    instrument_id = make_instrument_id(symbol)
    bars: list[DailyPriceBar] = []
    metadata = json.dumps(
        {"synthetic": True, "dataset": DATASET_ID, "version": DATASET_VERSION, "symbol": symbol},
        sort_keys=True,
    )
    for index, (day, close) in enumerate(zip(sessions, closes, strict=True)):
        previous = closes[index - 1] if index else close
        high = round(max(previous, close) * 1.003, 2)
        low = round(min(previous, close) * 0.997, 2)
        bars.append(
            DailyPriceBar(
                instrument_id=instrument_id,
                provider=MarketDataProviderName.TWELVE_DATA,
                trading_date=day,
                adjustment_mode=PriceAdjustmentMode.ALL,
                open=Decimal(str(previous)),
                high=Decimal(str(high)),
                low=Decimal(str(low)),
                close=Decimal(str(close)),
                volume=1_000_000 + (index * 7919) % 250_000,
                available_at=bar_available_at(day, "America/New_York"),
                fetched_at=FETCHED_AT,
                source_metadata_json=metadata,
            )
        )
    return tuple(bars)


def build_dataset() -> DemoDataset:
    """Build the full dataset in memory. Pure and deterministic."""
    sessions = trading_sessions()
    market = _market_returns(sessions)
    companies = tuple(_build_company(spec) for spec in COMPANIES)

    series: list[SeriesBundle] = []
    index_closes: list[float] = []
    level = 100.0
    for ret in market:
        level = round(level * (1.0 + ret), 2)
        index_closes.append(level)
    series.append(
        SeriesBundle(
            symbol=CALENDAR_SYMBOL,
            name=BENCHMARK_NAME,
            asset_type=AssetType.INDEX,
            issuer_cik=None,
            bars=_to_bars(index_closes, sessions, symbol=CALENDAR_SYMBOL),
        )
    )
    for spec in COMPANIES:
        closes = _price_path(
            spec.ticker,
            sessions,
            market,
            drift=spec.annual_drift,
            beta=spec.beta,
            idio_vol=spec.idio_vol,
            start_price=spec.start_price,
        )
        series.append(
            SeriesBundle(
                symbol=spec.ticker,
                name=spec.legal_name,
                asset_type=AssetType.EQUITY,
                issuer_cik=spec.cik,
                bars=_to_bars(closes, sessions, symbol=spec.ticker),
            )
        )
        for extra in spec.extra_tickers:
            drift, beta, vol, start = _CLASS_B[extra]
            series.append(
                SeriesBundle(
                    symbol=extra,
                    name=f"{spec.legal_name} (Class B)",
                    asset_type=AssetType.EQUITY,
                    issuer_cik=spec.cik,
                    bars=_to_bars(
                        _price_path(
                            extra,
                            sessions,
                            market,
                            drift=drift,
                            beta=beta,
                            idio_vol=vol,
                            start_price=start,
                        ),
                        sessions,
                        symbol=extra,
                    ),
                )
            )
    return DemoDataset(companies=companies, series=tuple(series))


def _ts(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _price(value: object) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.000001")))


def row_issuer(cik: str, legal_name: str, sic_description: str | None) -> str:
    return f"issuer|{cik}|{legal_name}|{sic_description}"


def row_security(ticker: str, cik: str, is_primary: bool, exchange: str | None) -> str:
    return f"security|{ticker}|{cik}|{int(is_primary)}|{exchange}"


def row_filing(
    accession: str,
    cik: str,
    form: str,
    filing_date: date,
    report_date: date | None,
    available_at: datetime,
) -> str:
    return (
        f"filing|{accession}|{cik}|{form}|{filing_date.isoformat()}|{report_date}|"
        f"{_ts(available_at)}"
    )


def row_fact(
    *,
    fact_id: str | None,
    cik: str,
    concept: str,
    value: float,
    start_date: date | None,
    end_date: date | None,
    accession: str | None,
    fiscal_year: int | None,
    fiscal_period: str | None,
    available_at: datetime,
    source_url: str | None,
) -> str:
    return (
        f"fact|{fact_id}|{cik}|{concept}|{float(value)!r}|{start_date}|{end_date}|"
        f"{accession}|{fiscal_year}|{fiscal_period}|{_ts(available_at)}|{source_url}"
    )


def row_instrument(
    symbol: str, asset_type: str, issuer_cik: str | None, security_ticker: str | None
) -> str:
    return f"instrument|{symbol}|{asset_type}|{issuer_cik}|{security_ticker}"


def row_bar(
    symbol: str,
    day: date,
    prices: tuple[object, object, object, object],
    volume: int,
    available_at: datetime,
) -> str:
    opened, high, low, close = (_price(item) for item in prices)
    return (
        f"bar|{symbol}|{day.isoformat()}|{opened}|{high}|{low}|{close}|{volume}|{_ts(available_at)}"
    )


def digest_rows(rows: list[str]) -> str:
    """Order-independent SHA-256 over canonical rows (no persistence timestamps)."""
    digest = hashlib.sha256()
    for row in sorted(rows):
        digest.update(row.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def dataset_rows(dataset: DemoDataset) -> list[str]:
    rows = [f"dataset|{DATASET_ID}|{DATASET_VERSION}"]
    for company in dataset.companies:
        issuer = company.issuer
        rows.append(row_issuer(issuer.cik, issuer.legal_name, issuer.sic_description))
        rows.extend(
            row_security(s.ticker, s.cik, s.is_primary, s.exchange) for s in company.securities
        )
        rows.extend(
            row_filing(
                f.accession_number, f.cik, f.form, f.filing_date, f.report_date, f.available_at
            )
            for f in company.filings
        )
        rows.extend(
            row_fact(
                fact_id=f.fact_id,
                cik=f.cik,
                concept=f.concept,
                value=f.value,
                start_date=f.start_date,
                end_date=f.end_date,
                accession=f.accession_number,
                fiscal_year=f.fiscal_year,
                fiscal_period=f.fiscal_period,
                available_at=f.available_at,
                source_url=f.source_url,
            )
            for f in company.facts
        )
    for series in dataset.series:
        security_ticker = series.symbol if series.asset_type is AssetType.EQUITY else None
        rows.append(
            row_instrument(
                series.symbol, series.asset_type.value, series.issuer_cik, security_ticker
            )
        )
        rows.extend(
            row_bar(
                series.symbol,
                bar.trading_date,
                (bar.open, bar.high, bar.low, bar.close),
                bar.volume,
                bar.available_at,
            )
            for bar in series.bars
        )
    return rows


def dataset_digest(dataset: DemoDataset) -> str:
    """SHA-256 over the canonical content of the dataset."""
    return digest_rows(dataset_rows(dataset))
