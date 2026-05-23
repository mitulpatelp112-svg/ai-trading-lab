"""Massive (massive.com) data adapter.

Implements the same function surface as ``src.tools.api`` (the original
financialdatasets.ai client) so the analyst agents can switch providers with
zero code changes. Selected via DATA_PROVIDER=massive in env.

Endpoints used:
  /v2/aggs/ticker/{t}/range/1/day/{from}/{to}   - OHLCV bars
  /stocks/financials/v1/ratios                  - valuation + profitability ratios
  /stocks/financials/v1/income-statements       - revenue, margins, EPS, EBITDA
  /stocks/financials/v1/balance-sheets          - assets, equity, debt
  /stocks/financials/v1/cash-flow-statements    - OCF, capex, dividends, D&A
  /v2/reference/news                            - news + sentiment insights
  /v3/reference/tickers/{t}                     - ticker overview
"""
import logging
import os
import time
from datetime import datetime, timedelta

import requests

from src.data.cache import get_cache
from src.data.models import (
    CompanyFacts,
    CompanyFactsResponse,
    CompanyNews,
    FinancialMetrics,
    InsiderTrade,
    LineItem,
    Price,
)

logger = logging.getLogger(__name__)
_cache = get_cache()

BASE_URL = os.environ.get("MASSIVE_BASE_URL", "https://api.massive.com")


def _headers(api_key: str | None) -> dict:
    key = api_key or os.environ.get("MASSIVE_API_KEY")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _request(path: str, params: dict | None = None, api_key: str | None = None, max_retries: int = 3) -> dict | None:
    url = f"{BASE_URL}{path}"
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, headers=_headers(api_key), params=params or {}, timeout=30)
        except requests.RequestException as e:
            logger.warning("Massive request error %s: %s", path, e)
            return None
        if resp.status_code == 429 and attempt < max_retries:
            time.sleep(15 + 15 * attempt)
            continue
        if resp.status_code != 200:
            logger.warning("Massive %s returned %s: %s", path, resp.status_code, resp.text[:200])
            return None
        try:
            return resp.json()
        except ValueError:
            return None
    return None


def _period_to_timeframe(period: str) -> str:
    p = (period or "ttm").lower()
    if p in ("ttm", "trailing_twelve_months"):
        return "trailing_twelve_months"
    if p in ("annual", "yearly", "year"):
        return "annual"
    return "quarterly"


def get_prices(ticker: str, start_date: str, end_date: str, api_key: str = None) -> list[Price]:
    cache_key = f"massive_{ticker}_{start_date}_{end_date}"
    if cached := _cache.get_prices(cache_key):
        return [Price(**p) for p in cached]

    path = f"/v2/aggs/ticker/{ticker}/range/1/day/{start_date}/{end_date}"
    data = _request(path, {"adjusted": "true", "sort": "asc", "limit": 50000}, api_key)
    if not data or not data.get("results"):
        return []

    prices: list[Price] = []
    for row in data["results"]:
        try:
            t = datetime.utcfromtimestamp(row["t"] / 1000).strftime("%Y-%m-%d")
            prices.append(Price(open=row["o"], close=row["c"], high=row["h"], low=row["l"], volume=int(row["v"]), time=t))
        except (KeyError, TypeError):
            continue

    _cache.set_prices(cache_key, [p.model_dump() for p in prices])
    return prices


def _fetch_financials(endpoint: str, ticker: str, end_date: str, period: str, limit: int, api_key: str | None) -> list[dict]:
    timeframe = _period_to_timeframe(period)
    params = {
        "ticker": ticker,
        "timeframe": timeframe,
        "period_end.lte": end_date,
        "limit": min(limit, 100),
        "order": "desc",
        "sort": "period_end",
    }
    data = _request(f"/stocks/financials/v1/{endpoint}", params, api_key)
    if not data:
        return []
    return data.get("results") or []


def _fetch_ratios(ticker: str, end_date: str, limit: int, api_key: str | None) -> list[dict]:
    params = {
        "ticker": ticker,
        "date.lte": end_date,
        "limit": min(limit, 100),
        "order": "desc",
        "sort": "date",
    }
    data = _request("/stocks/financials/v1/ratios", params, api_key)
    if not data:
        return []
    return data.get("results") or []


def _pct_change(series: list[float | None]) -> float | None:
    """Latest period-over-period growth from a descending-ordered series."""
    if len(series) < 2 or series[0] is None or series[1] in (None, 0):
        return None
    try:
        return (series[0] - series[1]) / abs(series[1])
    except (TypeError, ZeroDivisionError):
        return None


def get_financial_metrics(ticker: str, end_date: str, period: str = "ttm", limit: int = 10, api_key: str = None) -> list[FinancialMetrics]:
    cache_key = f"massive_{ticker}_{period}_{end_date}_{limit}"
    if cached := _cache.get_financial_metrics(cache_key):
        return [FinancialMetrics(**m) for m in cached]

    ratios = _fetch_ratios(ticker, end_date, limit, api_key)
    income = _fetch_financials("income-statements", ticker, end_date, period, limit + 4, api_key)
    cashflow = _fetch_financials("cash-flow-statements", ticker, end_date, period, limit + 4, api_key)
    balance = _fetch_financials("balance-sheets", ticker, end_date, period, limit + 4, api_key)

    if not income and not ratios:
        return []

    # Index income/cashflow/balance by period_end so we can align them
    by_period_inc = {r.get("period_end"): r for r in income if r.get("period_end")}
    by_period_cf = {r.get("period_end"): r for r in cashflow if r.get("period_end")}
    by_period_bs = {r.get("period_end"): r for r in balance if r.get("period_end")}

    sorted_periods = sorted(by_period_inc.keys(), reverse=True)[:limit]

    # Helpers for growth computation
    rev_series = [by_period_inc.get(p, {}).get("revenue") for p in sorted_periods]
    ni_series = [by_period_inc.get(p, {}).get("net_income_loss_attributable_common_shareholders") for p in sorted_periods]
    eps_series = [by_period_inc.get(p, {}).get("diluted_earnings_per_share") for p in sorted_periods]
    ebitda_series = [by_period_inc.get(p, {}).get("ebitda") for p in sorted_periods]
    op_series = [by_period_inc.get(p, {}).get("operating_income") for p in sorted_periods]
    fcf_series: list[float | None] = []
    book_series: list[float | None] = []
    for p in sorted_periods:
        cf = by_period_cf.get(p, {})
        ocf = cf.get("net_cash_from_operating_activities")
        capex = cf.get("purchase_of_property_plant_and_equipment")
        if ocf is not None and capex is not None:
            fcf_series.append(ocf + capex)  # capex is negative in their convention
        else:
            fcf_series.append(None)
        bs = by_period_bs.get(p, {})
        book_series.append(bs.get("total_equity_attributable_to_parent") or bs.get("total_equity"))

    latest_ratio = ratios[0] if ratios else {}

    out: list[FinancialMetrics] = []
    for idx, p_end in enumerate(sorted_periods):
        inc = by_period_inc.get(p_end, {})
        bs = by_period_bs.get(p_end, {})
        cf = by_period_cf.get(p_end, {})
        r = latest_ratio if idx == 0 else {}

        revenue = inc.get("revenue")
        gross = inc.get("gross_profit")
        op_inc = inc.get("operating_income")
        net_inc = inc.get("net_income_loss_attributable_common_shareholders")
        total_assets = bs.get("total_assets")
        total_liab = bs.get("total_liabilities")
        equity = bs.get("total_equity_attributable_to_parent") or bs.get("total_equity")
        cur_assets = bs.get("total_current_assets")
        cur_liab = bs.get("total_current_liabilities")
        cash_eq = bs.get("cash_and_equivalents")
        inventories = bs.get("inventories") or 0
        debt = (bs.get("debt_current") or 0) + (bs.get("long_term_debt_and_capital_lease_obligations") or 0)
        shares = inc.get("diluted_shares_outstanding")
        ocf = cf.get("net_cash_from_operating_activities")
        capex = cf.get("purchase_of_property_plant_and_equipment")
        fcf = (ocf + capex) if (ocf is not None and capex is not None) else None

        def _safe_div(n, d):
            if n is None or d in (None, 0):
                return None
            return n / d

        out.append(FinancialMetrics(
            ticker=ticker,
            report_period=p_end,
            period=period,
            currency="USD",
            market_cap=r.get("market_cap") if idx == 0 else None,
            enterprise_value=r.get("enterprise_value") if idx == 0 else None,
            price_to_earnings_ratio=r.get("price_to_earnings") if idx == 0 else None,
            price_to_book_ratio=r.get("price_to_book") if idx == 0 else None,
            price_to_sales_ratio=r.get("price_to_sales") if idx == 0 else None,
            enterprise_value_to_ebitda_ratio=r.get("ev_to_ebitda") if idx == 0 else None,
            enterprise_value_to_revenue_ratio=r.get("ev_to_sales") if idx == 0 else None,
            free_cash_flow_yield=_safe_div(fcf, r.get("market_cap")) if (idx == 0 and fcf is not None) else None,
            peg_ratio=None,
            gross_margin=_safe_div(gross, revenue),
            operating_margin=_safe_div(op_inc, revenue),
            net_margin=_safe_div(net_inc, revenue),
            return_on_equity=_safe_div(net_inc, equity),
            return_on_assets=_safe_div(net_inc, total_assets),
            return_on_invested_capital=_safe_div(net_inc, (equity or 0) + debt) if (equity or debt) else None,
            asset_turnover=_safe_div(revenue, total_assets),
            inventory_turnover=_safe_div(inc.get("cost_of_revenue"), inventories) if inventories else None,
            receivables_turnover=_safe_div(revenue, bs.get("receivables")),
            days_sales_outstanding=_safe_div(bs.get("receivables"), revenue) * 365 if (bs.get("receivables") and revenue) else None,
            operating_cycle=None,
            working_capital_turnover=_safe_div(revenue, ((cur_assets or 0) - (cur_liab or 0))) if (cur_assets and cur_liab) else None,
            current_ratio=_safe_div(cur_assets, cur_liab),
            quick_ratio=_safe_div((cur_assets or 0) - inventories, cur_liab) if cur_liab else None,
            cash_ratio=_safe_div(cash_eq, cur_liab),
            operating_cash_flow_ratio=_safe_div(ocf, cur_liab),
            debt_to_equity=_safe_div(debt, equity),
            debt_to_assets=_safe_div(debt, total_assets),
            interest_coverage=_safe_div(op_inc, inc.get("interest_expense")) if inc.get("interest_expense") else None,
            revenue_growth=_pct_change(rev_series[idx:idx + 2]) if idx + 1 < len(rev_series) else None,
            earnings_growth=_pct_change(ni_series[idx:idx + 2]) if idx + 1 < len(ni_series) else None,
            book_value_growth=_pct_change(book_series[idx:idx + 2]) if idx + 1 < len(book_series) else None,
            earnings_per_share_growth=_pct_change(eps_series[idx:idx + 2]) if idx + 1 < len(eps_series) else None,
            free_cash_flow_growth=_pct_change(fcf_series[idx:idx + 2]) if idx + 1 < len(fcf_series) else None,
            operating_income_growth=_pct_change(op_series[idx:idx + 2]) if idx + 1 < len(op_series) else None,
            ebitda_growth=_pct_change(ebitda_series[idx:idx + 2]) if idx + 1 < len(ebitda_series) else None,
            payout_ratio=_safe_div(-(cf.get("dividends") or 0), net_inc) if (net_inc and cf.get("dividends") is not None) else None,
            earnings_per_share=inc.get("diluted_earnings_per_share") or inc.get("basic_earnings_per_share"),
            book_value_per_share=_safe_div(equity, shares),
            free_cash_flow_per_share=_safe_div(fcf, shares),
        ))

    if not out:
        return []
    _cache.set_financial_metrics(cache_key, [m.model_dump() for m in out])
    return out


# Map agent-requested line item names → (statement, field) in Massive responses.
_LINE_ITEM_MAP: dict[str, tuple[str, str]] = {
    # income statement
    "revenue": ("income", "revenue"),
    "gross_profit": ("income", "gross_profit"),
    "operating_income": ("income", "operating_income"),
    "operating_expense": ("income", "total_operating_expenses"),
    "operating_expenses": ("income", "total_operating_expenses"),
    "ebit": ("income", "operating_income"),
    "ebitda": ("income", "ebitda"),
    "net_income": ("income", "net_income_loss_attributable_common_shareholders"),
    "research_and_development": ("income", "research_development"),
    "selling_general_and_administrative_expenses": ("income", "selling_general_administrative"),
    "interest_expense": ("income", "interest_expense"),
    "income_tax_expense": ("income", "income_taxes"),
    "outstanding_shares": ("income", "diluted_shares_outstanding"),
    "diluted_shares": ("income", "diluted_shares_outstanding"),
    "earnings_per_share": ("income", "diluted_earnings_per_share"),
    "cost_of_revenue": ("income", "cost_of_revenue"),
    # balance sheet
    "total_assets": ("balance", "total_assets"),
    "total_liabilities": ("balance", "total_liabilities"),
    "shareholders_equity": ("balance", "total_equity_attributable_to_parent"),
    "current_assets": ("balance", "total_current_assets"),
    "current_liabilities": ("balance", "total_current_liabilities"),
    "cash_and_equivalents": ("balance", "cash_and_equivalents"),
    "short_term_investments": ("balance", "short_term_investments"),
    "inventory": ("balance", "inventories"),
    "accounts_receivable": ("balance", "receivables"),
    "accounts_payable": ("balance", "accounts_payable"),
    "goodwill_and_intangible_assets": ("balance", "goodwill"),
    "intangible_assets": ("balance", "intangible_assets_net"),
    "property_plant_and_equipment": ("balance", "property_plant_equipment_net"),
    "retained_earnings": ("balance", "retained_earnings_deficit"),
    # cash flow
    "capital_expenditure": ("cashflow", "purchase_of_property_plant_and_equipment"),
    "depreciation_and_amortization": ("cashflow", "depreciation_depletion_and_amortization"),
    "dividends_and_other_cash_distributions": ("cashflow", "dividends"),
    "issuance_or_purchase_of_equity_shares": ("cashflow", "other_financing_activities"),
    "net_cash_from_operating_activities": ("cashflow", "net_cash_from_operating_activities"),
    "net_cash_from_financing_activities": ("cashflow", "net_cash_from_financing_activities"),
    "net_cash_from_investing_activities": ("cashflow", "net_cash_from_investing_activities"),
}


def search_line_items(ticker: str, line_items: list[str], end_date: str, period: str = "ttm", limit: int = 10, api_key: str = None) -> list[LineItem]:
    income = _fetch_financials("income-statements", ticker, end_date, period, limit + 4, api_key)
    balance = _fetch_financials("balance-sheets", ticker, end_date, period, limit + 4, api_key)
    cashflow = _fetch_financials("cash-flow-statements", ticker, end_date, period, limit + 4, api_key)

    by_period = {}

    def _ingest(statement_key: str, rows: list[dict]):
        for row in rows:
            pe = row.get("period_end")
            if not pe:
                continue
            slot = by_period.setdefault(pe, {"income": {}, "balance": {}, "cashflow": {}, "currency": "USD"})
            slot[statement_key] = row

    _ingest("income", income)
    _ingest("balance", balance)
    _ingest("cashflow", cashflow)

    sorted_periods = sorted(by_period.keys(), reverse=True)[:limit]
    results: list[LineItem] = []

    # Compute free_cash_flow per period if requested
    needs_fcf = "free_cash_flow" in line_items
    needs_wc = "working_capital" in line_items
    needs_long_term_debt = "long_term_debt" in line_items
    needs_total_debt = "total_debt" in line_items

    for pe in sorted_periods:
        slot = by_period[pe]
        fields = {}
        for item in line_items:
            if item == "free_cash_flow":
                ocf = slot["cashflow"].get("net_cash_from_operating_activities")
                capex = slot["cashflow"].get("purchase_of_property_plant_and_equipment")
                fields["free_cash_flow"] = (ocf + capex) if (ocf is not None and capex is not None) else None
            elif item == "working_capital":
                ca = slot["balance"].get("total_current_assets")
                cl = slot["balance"].get("total_current_liabilities")
                fields["working_capital"] = (ca - cl) if (ca is not None and cl is not None) else None
            elif item == "long_term_debt":
                fields["long_term_debt"] = slot["balance"].get("long_term_debt_and_capital_lease_obligations")
            elif item == "total_debt":
                cd = slot["balance"].get("debt_current") or 0
                ld = slot["balance"].get("long_term_debt_and_capital_lease_obligations") or 0
                fields["total_debt"] = cd + ld if (cd or ld) else None
            elif item in _LINE_ITEM_MAP:
                stmt, field = _LINE_ITEM_MAP[item]
                fields[item] = slot[stmt].get(field)
            else:
                fields[item] = None

        results.append(LineItem(
            ticker=ticker,
            report_period=pe,
            period=period,
            currency="USD",
            **fields,
        ))
    return results


def get_insider_trades(ticker: str, end_date: str, start_date: str | None = None, limit: int = 1000, api_key: str = None) -> list[InsiderTrade]:
    # Massive has Form 4 endpoints but mapping is non-trivial; return empty so agents fall back gracefully.
    return []


def get_company_news(ticker: str, end_date: str, start_date: str | None = None, limit: int = 1000, api_key: str = None) -> list[CompanyNews]:
    cache_key = f"massive_news_{ticker}_{start_date or 'none'}_{end_date}_{limit}"
    if cached := _cache.get_company_news(cache_key):
        return [CompanyNews(**n) for n in cached]

    params = {
        "ticker": ticker,
        "published_utc.lte": end_date,
        "order": "desc",
        "sort": "published_utc",
        "limit": min(limit, 1000),
    }
    if start_date:
        params["published_utc.gte"] = start_date

    data = _request("/v2/reference/news", params, api_key)
    if not data or not data.get("results"):
        return []

    news: list[CompanyNews] = []
    for row in data["results"]:
        try:
            # Pick sentiment from the insight tagged to this ticker, if any
            sentiment = None
            for insight in row.get("insights") or []:
                if insight.get("ticker") == ticker:
                    sentiment = insight.get("sentiment")
                    break
            news.append(CompanyNews(
                ticker=ticker,
                title=row.get("title") or "",
                author=row.get("author"),
                source=(row.get("publisher") or {}).get("name") or "unknown",
                date=row.get("published_utc") or "",
                url=row.get("article_url") or "",
                sentiment=sentiment,
            ))
        except Exception as e:
            logger.debug("Skipping news row: %s", e)
            continue

    _cache.set_company_news(cache_key, [n.model_dump() for n in news])
    return news


def get_market_cap(ticker: str, end_date: str, api_key: str = None) -> float | None:
    metrics = get_financial_metrics(ticker, end_date, period="ttm", limit=1, api_key=api_key)
    if metrics and metrics[0].market_cap:
        return metrics[0].market_cap
    # Fallback: ticker overview
    data = _request(f"/v3/reference/tickers/{ticker}", {"date": end_date}, api_key)
    if data and data.get("results"):
        return data["results"].get("market_cap")
    return None


def get_company_facts(ticker: str, api_key: str = None) -> CompanyFactsResponse | None:
    data = _request(f"/v3/reference/tickers/{ticker}", api_key=api_key)
    if not data or not data.get("results"):
        return None
    r = data["results"]
    return CompanyFactsResponse(company_facts=CompanyFacts(
        ticker=r.get("ticker", ticker),
        name=r.get("name", ticker),
        cik=r.get("cik"),
        sector=(r.get("sic_description") or None),
        exchange=r.get("primary_exchange"),
        is_active=r.get("active"),
        listing_date=r.get("list_date"),
        market_cap=r.get("market_cap"),
        weighted_average_shares=r.get("weighted_shares_outstanding"),
    ))
