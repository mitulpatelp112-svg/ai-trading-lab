"""Finnhub + Alpaca hybrid data adapter.

Alpaca handles historical price bars (free, IEX feed).
Finnhub handles fundamentals, news, insider trades, market cap.

Routed in api.py when DATA_PROVIDER=finnhub.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import requests

from src.data.models import (
    CompanyNews,
    FinancialMetrics,
    InsiderTrade,
    LineItem,
    Price,
)

logger = logging.getLogger(__name__)

_FINNHUB = "https://finnhub.io/api/v1"
_ALPACA_DATA = "https://data.alpaca.markets"


def _finnhub_key() -> str:
    return os.environ.get("FINNHUB_API_KEY", "").strip()


def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", "").strip(),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", "").strip(),
    }


def _get(url: str, params: dict | None = None, headers: dict | None = None) -> Any:
    """GET with retry on rate limits."""
    for attempt in range(3):
        try:
            r = requests.get(url, params=params or {}, headers=headers or {}, timeout=20)
        except requests.RequestException as e:
            logger.warning("HTTP request failed: %s", e)
            return None
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                return None
        if r.status_code == 429:
            time.sleep(2 + attempt * 3)
            continue
        if r.status_code in (401, 403, 404):
            return None
        time.sleep(1)
    return None


# ---------------------------------------------------------------------------
# Prices — Alpaca IEX (free)
# ---------------------------------------------------------------------------
def get_prices(ticker: str, start_date: str, end_date: str, api_key: str | None = None) -> list[Price]:
    url = f"{_ALPACA_DATA}/v2/stocks/{ticker}/bars"
    params = {
        "timeframe": "1Day",
        "start": start_date,
        "end": end_date,
        "feed": "iex",
        "limit": 10000,
        "adjustment": "raw",
    }
    j = _get(url, params=params, headers=_alpaca_headers())
    bars = (j or {}).get("bars") or []
    out: list[Price] = []
    for b in bars:
        try:
            out.append(Price(
                open=float(b["o"]),
                high=float(b["h"]),
                low=float(b["l"]),
                close=float(b["c"]),
                volume=int(b.get("v", 0) or 0),
                time=b["t"],
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return out


# ---------------------------------------------------------------------------
# Financial metrics — Finnhub /stock/metric
# ---------------------------------------------------------------------------
def _safe(d: dict, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v is not None and v != 0 and v != "":
            try:
                return float(v)
            except (TypeError, ValueError):
                return v
    return default


def get_financial_metrics(
    ticker: str,
    end_date: str,
    period: str = "ttm",
    limit: int = 10,
    api_key: str | None = None,
) -> list[FinancialMetrics]:
    j = _get(f"{_FINNHUB}/stock/metric", params={"symbol": ticker, "metric": "all", "token": _finnhub_key()})
    if not j:
        return []
    m = j.get("metric") or {}

    metric = FinancialMetrics(
        ticker=ticker,
        report_period=end_date,
        period=period,
        currency="USD",
        market_cap=_safe(m, "marketCapitalization") and _safe(m, "marketCapitalization") * 1_000_000,  # finnhub gives in millions
        enterprise_value=_safe(m, "enterpriseValue"),
        price_to_earnings_ratio=_safe(m, "peNormalizedAnnual", "peBasicExclExtraTTM", "peExclExtraAnnual"),
        price_to_book_ratio=_safe(m, "pbAnnual", "pbQuarterly"),
        price_to_sales_ratio=_safe(m, "psAnnual", "psTTM"),
        enterprise_value_to_ebitda_ratio=_safe(m, "currentEv/freeCashFlowAnnual"),
        enterprise_value_to_revenue_ratio=None,
        free_cash_flow_yield=None,
        peg_ratio=_safe(m, "pegRatio"),
        gross_margin=_safe(m, "grossMarginAnnual", "grossMarginTTM"),
        operating_margin=_safe(m, "operatingMarginAnnual", "operatingMarginTTM"),
        net_margin=_safe(m, "netProfitMarginAnnual", "netProfitMarginTTM"),
        return_on_equity=_safe(m, "roeRfy", "roeTTM"),
        return_on_assets=_safe(m, "roaRfy", "roaTTM"),
        return_on_invested_capital=_safe(m, "roiAnnual", "roiTTM"),
        asset_turnover=_safe(m, "assetTurnoverAnnual", "assetTurnoverTTM"),
        inventory_turnover=_safe(m, "inventoryTurnoverAnnual", "inventoryTurnoverTTM"),
        receivables_turnover=_safe(m, "receivablesTurnoverAnnual", "receivablesTurnoverTTM"),
        days_sales_outstanding=None,
        operating_cycle=None,
        working_capital_turnover=None,
        current_ratio=_safe(m, "currentRatioAnnual", "currentRatioQuarterly"),
        quick_ratio=_safe(m, "quickRatioAnnual", "quickRatioQuarterly"),
        cash_ratio=None,
        operating_cash_flow_ratio=None,
        debt_to_equity=_safe(m, "totalDebt/totalEquityAnnual", "totalDebt/totalEquityQuarterly"),
        debt_to_assets=_safe(m, "longTermDebt/equityAnnual"),
        interest_coverage=None,
        revenue_growth=_safe(m, "revenueGrowthTTMYoy", "revenueGrowth5Y"),
        earnings_growth=_safe(m, "epsGrowthTTMYoy", "epsGrowth5Y"),
        book_value_growth=_safe(m, "bookValueShareGrowth5Y"),
        earnings_per_share_growth=_safe(m, "epsGrowth5Y"),
        free_cash_flow_growth=None,
        operating_income_growth=None,
        ebitda_growth=None,
        payout_ratio=_safe(m, "payoutRatioAnnual", "payoutRatioTTM"),
        earnings_per_share=_safe(m, "epsTTM", "epsAnnual"),
        book_value_per_share=_safe(m, "bookValuePerShareAnnual", "bookValuePerShareQuarterly"),
        free_cash_flow_per_share=None,
    )
    return [metric]


# ---------------------------------------------------------------------------
# Line items — Finnhub /stock/financials-reported
# ---------------------------------------------------------------------------
# Map of internal line_item names → list of (statement_type, candidate_concept_names)
_LINE_ITEM_MAP = {
    "revenue": ("ic", ["Revenues", "SalesRevenueNet", "Revenue"]),
    "net_income": ("ic", ["NetIncomeLoss", "ProfitLoss"]),
    "operating_income": ("ic", ["OperatingIncomeLoss", "IncomeFromOperations"]),
    "gross_profit": ("ic", ["GrossProfit"]),
    "ebitda": ("ic", ["EBITDA"]),
    "earnings_per_share": ("ic", ["EarningsPerShareBasic", "EarningsPerShareDiluted"]),
    "free_cash_flow": ("cf", ["FreeCashFlow"]),
    "operating_cash_flow": ("cf", ["NetCashProvidedByUsedInOperatingActivities"]),
    "capital_expenditure": ("cf", ["PaymentsToAcquirePropertyPlantAndEquipment"]),
    "cash_and_equivalents": ("bs", ["CashAndCashEquivalentsAtCarryingValue", "Cash"]),
    "total_debt": ("bs", ["LongTermDebt", "LongTermDebtNoncurrent"]),
    "total_assets": ("bs", ["Assets"]),
    "total_liabilities": ("bs", ["Liabilities"]),
    "shareholders_equity": ("bs", ["StockholdersEquity"]),
    "outstanding_shares": ("bs", ["CommonStockSharesOutstanding"]),
    "research_and_development": ("ic", ["ResearchAndDevelopmentExpense"]),
    "depreciation_and_amortization": ("cf", ["DepreciationDepletionAndAmortization"]),
    "working_capital": ("bs", []),  # derived
    "current_assets": ("bs", ["AssetsCurrent"]),
    "current_liabilities": ("bs", ["LiabilitiesCurrent"]),
    "interest_expense": ("ic", ["InterestExpense"]),
}


def search_line_items(
    ticker: str,
    line_items: list[str],
    end_date: str,
    period: str = "ttm",
    limit: int = 10,
    api_key: str | None = None,
) -> list[LineItem]:
    freq = "annual" if period in ("annual", "fy") else "quarterly"
    j = _get(f"{_FINNHUB}/stock/financials-reported",
             params={"symbol": ticker, "freq": freq, "token": _finnhub_key()})
    if not j:
        return []

    reports = j.get("data") or []
    out: list[LineItem] = []
    for report in reports[:limit]:
        end_date_str = report.get("endDate", "")
        if not end_date_str or end_date_str > end_date:
            continue

        report_data = report.get("report") or {}
        # Flatten all concepts across ic/bs/cf into a single lookup
        concept_values: dict[tuple[str, str], float] = {}
        for stmt_key in ("ic", "bs", "cf"):
            for row in report_data.get(stmt_key, []) or []:
                concept = row.get("concept", "")
                val = row.get("value")
                if concept and val is not None:
                    try:
                        concept_values[(stmt_key, concept)] = float(val)
                    except (TypeError, ValueError):
                        continue

        item_data: dict[str, Any] = {
            "ticker": ticker,
            "report_period": end_date_str,
            "period": period,
            "currency": "USD",
        }
        for key in line_items:
            mapping = _LINE_ITEM_MAP.get(key)
            if not mapping:
                item_data[key] = None
                continue
            stmt_type, candidates = mapping
            val = None
            for candidate in candidates:
                v = concept_values.get((stmt_type, candidate))
                if v is not None:
                    val = v
                    break
            item_data[key] = val

        try:
            out.append(LineItem(**item_data))
        except Exception:
            base = {k: item_data[k] for k in ("ticker", "report_period", "period", "currency")}
            out.append(LineItem(**base))

    return out


# ---------------------------------------------------------------------------
# Insider trades — Finnhub
# ---------------------------------------------------------------------------
def get_insider_trades(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 1000,
    api_key: str | None = None,
) -> list[InsiderTrade]:
    params: dict = {"symbol": ticker, "token": _finnhub_key()}
    if start_date:
        params["from"] = start_date
    if end_date:
        params["to"] = end_date
    j = _get(f"{_FINNHUB}/stock/insider-transactions", params=params)
    if not j:
        return []
    out: list[InsiderTrade] = []
    for t in (j.get("data") or [])[:limit]:
        try:
            shares = float(t.get("share", 0) or 0)
            change = float(t.get("change", 0) or 0)
            price = float(t.get("transactionPrice", 0) or 0)
            tx_date = t.get("transactionDate") or t.get("filingDate") or end_date
            kwargs = dict(
                ticker=ticker,
                issuer=ticker,
                name=t.get("name") or "",
                title=t.get("transactionCode") or "",
                is_board_director=None,
                transaction_date=tx_date,
                transaction_shares=change or shares,
                transaction_price_per_share=price,
                transaction_value=(change or shares) * price if price else None,
                shares_owned_before_transaction=None,
                shares_owned_after_transaction=shares,
                security_title=None,
                filing_date=t.get("filingDate") or tx_date,
            )
            out.append(InsiderTrade(**kwargs))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Company news — Finnhub
# ---------------------------------------------------------------------------
def get_company_news(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 1000,
    api_key: str | None = None,
) -> list[CompanyNews]:
    if not start_date:
        # Finnhub requires a from date — default to 90 days before end
        from datetime import timedelta
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            start_date = (end_dt - timedelta(days=90)).strftime("%Y-%m-%d")
        except ValueError:
            start_date = end_date

    j = _get(f"{_FINNHUB}/company-news",
             params={"symbol": ticker, "from": start_date, "to": end_date, "token": _finnhub_key()})
    if not isinstance(j, list):
        return []

    out: list[CompanyNews] = []
    for item in j[:limit]:
        try:
            ts = item.get("datetime")
            if isinstance(ts, (int, float)):
                date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                date_str = end_date
            out.append(CompanyNews(
                ticker=ticker,
                title=item.get("headline") or "",
                author=item.get("source") or "",
                source=item.get("source") or "",
                date=date_str,
                url=item.get("url") or "",
                sentiment=None,
            ))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Market cap — from /stock/metric (marketCapitalization in millions)
# ---------------------------------------------------------------------------
def get_market_cap(ticker: str, end_date: str, api_key: str | None = None) -> float | None:
    j = _get(f"{_FINNHUB}/stock/metric", params={"symbol": ticker, "metric": "all", "token": _finnhub_key()})
    if not j:
        return None
    m = (j.get("metric") or {})
    mc = m.get("marketCapitalization")
    if mc is None:
        return None
    try:
        return float(mc) * 1_000_000  # finnhub reports in millions
    except (TypeError, ValueError):
        return None
