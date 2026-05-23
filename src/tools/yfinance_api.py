"""yfinance-based data adapter.

Implements the same function surface as src/tools/api.py using the free
yfinance library, so backtests can run on any ticker yfinance knows about
(not just the 5 stocks FD free tier covers).

Routed in api.py when DATA_PROVIDER=yfinance.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pandas as pd
import yfinance as yf

# Use curl_cffi to impersonate Chrome — bypasses Yahoo's "Invalid Crumb" blocking.
# Sessions are NOT shared across threads (LangGraph fires agents in parallel and
# a shared session causes deadlocks). Each call gets a fresh session.
import threading

try:
    from curl_cffi import requests as _cffi_requests
    _HAS_CFFI = True
except Exception:
    _HAS_CFFI = False

_thread_local = threading.local()


def _get_session():
    if not _HAS_CFFI:
        return None
    sess = getattr(_thread_local, "session", None)
    if sess is None:
        sess = _cffi_requests.Session(impersonate="chrome")
        _thread_local.session = sess
    return sess


def _ticker(sym: str):
    """Build a yf.Ticker with a thread-local impersonated session."""
    return yf.Ticker(sym, session=_get_session())


from src.data.models import (
    CompanyNews,
    FinancialMetrics,
    InsiderTrade,
    LineItem,
    Price,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------
def get_prices(ticker: str, start_date: str, end_date: str, api_key: str | None = None) -> list[Price]:
    try:
        # Use Ticker.history() with our impersonated session for crumb-bypass
        df = _ticker(ticker).history(
            start=start_date, end=end_date, interval="1d", auto_adjust=False,
        )
    except Exception as e:
        logger.warning("yfinance download failed for %s: %s", ticker, e)
        return []

    if df is None or df.empty:
        return []

    # yfinance returns MultiIndex columns when downloading single ticker sometimes
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)

    out: list[Price] = []
    for ts, row in df.iterrows():
        try:
            out.append(Price(
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=int(row["Volume"]) if not pd.isna(row["Volume"]) else 0,
                time=ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return out


# ---------------------------------------------------------------------------
# Financial metrics (ratios)
# ---------------------------------------------------------------------------
def _safe(d: dict, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v is not None and v != 0:
            return float(v) if isinstance(v, (int, float)) else v
    return default


def get_financial_metrics(
    ticker: str,
    end_date: str,
    period: str = "ttm",
    limit: int = 10,
    api_key: str | None = None,
) -> list[FinancialMetrics]:
    try:
        info = _ticker(ticker).info or {}
    except Exception as e:
        logger.warning("yfinance info failed for %s: %s", ticker, e)
        return []

    if not info:
        return []

    metric = FinancialMetrics(
        ticker=ticker,
        report_period=end_date,
        period=period,
        currency=info.get("currency") or "USD",
        market_cap=_safe(info, "marketCap"),
        enterprise_value=_safe(info, "enterpriseValue"),
        price_to_earnings_ratio=_safe(info, "trailingPE", "forwardPE"),
        price_to_book_ratio=_safe(info, "priceToBook"),
        price_to_sales_ratio=_safe(info, "priceToSalesTrailing12Months"),
        enterprise_value_to_ebitda_ratio=_safe(info, "enterpriseToEbitda"),
        enterprise_value_to_revenue_ratio=_safe(info, "enterpriseToRevenue"),
        free_cash_flow_yield=None,
        peg_ratio=_safe(info, "trailingPegRatio", "pegRatio"),
        gross_margin=_safe(info, "grossMargins"),
        operating_margin=_safe(info, "operatingMargins"),
        net_margin=_safe(info, "profitMargins"),
        return_on_equity=_safe(info, "returnOnEquity"),
        return_on_assets=_safe(info, "returnOnAssets"),
        return_on_invested_capital=None,
        asset_turnover=None,
        inventory_turnover=None,
        receivables_turnover=None,
        days_sales_outstanding=None,
        operating_cycle=None,
        working_capital_turnover=None,
        current_ratio=_safe(info, "currentRatio"),
        quick_ratio=_safe(info, "quickRatio"),
        cash_ratio=None,
        operating_cash_flow_ratio=None,
        debt_to_equity=_safe(info, "debtToEquity"),
        debt_to_assets=None,
        interest_coverage=None,
        revenue_growth=_safe(info, "revenueGrowth"),
        earnings_growth=_safe(info, "earningsGrowth", "earningsQuarterlyGrowth"),
        book_value_growth=None,
        earnings_per_share_growth=None,
        free_cash_flow_growth=None,
        operating_income_growth=None,
        ebitda_growth=None,
        payout_ratio=_safe(info, "payoutRatio"),
        earnings_per_share=_safe(info, "trailingEps", "forwardEps"),
        book_value_per_share=_safe(info, "bookValue"),
        free_cash_flow_per_share=None,
    )
    return [metric]


# ---------------------------------------------------------------------------
# Line items (income/balance/cashflow rows)
# ---------------------------------------------------------------------------
_LINE_ITEM_MAP = {
    "revenue": ("income_stmt", "Total Revenue"),
    "net_income": ("income_stmt", "Net Income"),
    "operating_income": ("income_stmt", "Operating Income"),
    "gross_profit": ("income_stmt", "Gross Profit"),
    "ebitda": ("income_stmt", "EBITDA"),
    "earnings_per_share": ("income_stmt", "Basic EPS"),
    "free_cash_flow": ("cashflow", "Free Cash Flow"),
    "operating_cash_flow": ("cashflow", "Operating Cash Flow"),
    "capital_expenditure": ("cashflow", "Capital Expenditure"),
    "cash_and_equivalents": ("balance_sheet", "Cash And Cash Equivalents"),
    "total_debt": ("balance_sheet", "Total Debt"),
    "total_assets": ("balance_sheet", "Total Assets"),
    "total_liabilities": ("balance_sheet", "Total Liabilities Net Minority Interest"),
    "shareholders_equity": ("balance_sheet", "Stockholders Equity"),
    "outstanding_shares": ("balance_sheet", "Ordinary Shares Number"),
    "research_and_development": ("income_stmt", "Research And Development"),
    "depreciation_and_amortization": ("cashflow", "Depreciation And Amortization"),
    "working_capital": ("balance_sheet", "Working Capital"),
    "current_assets": ("balance_sheet", "Current Assets"),
    "current_liabilities": ("balance_sheet", "Current Liabilities"),
    "interest_expense": ("income_stmt", "Interest Expense"),
}


def search_line_items(
    ticker: str,
    line_items: list[str],
    end_date: str,
    period: str = "ttm",
    limit: int = 10,
    api_key: str | None = None,
) -> list[LineItem]:
    try:
        t = _ticker(ticker)
        stmts = {
            "income_stmt": t.income_stmt,
            "balance_sheet": t.balance_sheet,
            "cashflow": t.cashflow,
        }
    except Exception as e:
        logger.warning("yfinance statements failed for %s: %s", ticker, e)
        return []

    # Find common date columns across statements
    out: list[LineItem] = []
    all_dates: set = set()
    for df in stmts.values():
        if df is not None and not df.empty:
            all_dates.update(df.columns.tolist())

    sorted_dates = sorted([d for d in all_dates if d is not None], reverse=True)[:limit]

    for col_date in sorted_dates:
        date_str = col_date.strftime("%Y-%m-%d") if hasattr(col_date, "strftime") else str(col_date)
        if date_str > end_date:
            continue
        item_data: dict[str, Any] = {
            "ticker": ticker,
            "report_period": date_str,
            "period": period,
            "currency": "USD",
        }
        for key in line_items:
            mapping = _LINE_ITEM_MAP.get(key)
            if not mapping:
                item_data[key] = None
                continue
            stmt_name, row_name = mapping
            df = stmts.get(stmt_name)
            if df is None or df.empty or row_name not in df.index or col_date not in df.columns:
                item_data[key] = None
                continue
            val = df.at[row_name, col_date]
            try:
                item_data[key] = float(val) if pd.notna(val) else None
            except (ValueError, TypeError):
                item_data[key] = None

        # LineItem is a flexible dict-like Pydantic model — pass extra fields
        try:
            out.append(LineItem(**item_data))
        except Exception:
            # Fallback: only include known fields
            base = {k: item_data[k] for k in ("ticker", "report_period", "period", "currency")}
            out.append(LineItem(**base))

    return out


# ---------------------------------------------------------------------------
# Insider trades — yfinance has limited support, return empty list as fallback
# ---------------------------------------------------------------------------
def get_insider_trades(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 1000,
    api_key: str | None = None,
) -> list[InsiderTrade]:
    return []


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------
def get_company_news(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 1000,
    api_key: str | None = None,
) -> list[CompanyNews]:
    try:
        items = _ticker(ticker).news or []
    except Exception as e:
        logger.warning("yfinance news failed for %s: %s", ticker, e)
        return []

    out: list[CompanyNews] = []
    for item in items[:limit]:
        try:
            content = item.get("content") or item
            pub_time = content.get("pubDate") or content.get("providerPublishTime")
            if isinstance(pub_time, int):
                pub_dt = datetime.fromtimestamp(pub_time).strftime("%Y-%m-%dT%H:%M:%SZ")
            elif isinstance(pub_time, str):
                pub_dt = pub_time
            else:
                pub_dt = end_date

            out.append(CompanyNews(
                ticker=ticker,
                title=content.get("title") or "",
                author=(content.get("provider") or {}).get("displayName") if isinstance(content.get("provider"), dict) else (content.get("publisher") or ""),
                source=(content.get("provider") or {}).get("displayName") if isinstance(content.get("provider"), dict) else (content.get("publisher") or ""),
                date=pub_dt,
                url=(content.get("canonicalUrl") or {}).get("url") if isinstance(content.get("canonicalUrl"), dict) else content.get("link", ""),
                sentiment=None,
            ))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Market cap
# ---------------------------------------------------------------------------
def get_market_cap(ticker: str, end_date: str, api_key: str | None = None) -> float | None:
    try:
        info = _ticker(ticker).info or {}
        return float(info.get("marketCap")) if info.get("marketCap") else None
    except Exception:
        return None
