"""Local HTML dashboard — pinned at http://localhost:8765.

Run with:
    .venv/bin/python -m src.dashboard.server

Auto-refreshes every 30s. Pulls live data from Alpaca + Massive + Alpaca news.
"""
import logging
import os
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

load_dotenv(override=True)
logger = logging.getLogger("dashboard")

ALPACA_BROKER = "https://paper-api.alpaca.markets"
ALPACA_DATA = "https://data.alpaca.markets"

app = FastAPI()
ROOT = Path(__file__).parent
INDEX = ROOT / "index.html"


def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", "").strip(),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", "").strip(),
    }


def _get(url: str, params: dict | None = None) -> dict | list | None:
    try:
        r = requests.get(url, headers=_alpaca_headers(), params=params or {}, timeout=15)
    except requests.RequestException as e:
        logger.warning("Alpaca request failed: %s", e)
        return None
    if r.status_code != 200:
        return None
    return r.json()


def _fetch_portfolio_history() -> dict:
    """Equity time series since account inception (or last 1Y, whichever is shorter)."""
    j = _get(f"{ALPACA_BROKER}/v2/account/portfolio/history", {"period": "1A", "timeframe": "1D"})
    return j or {}


def _fetch_spy_history(start_iso: str, end_iso: str) -> list[dict]:
    """Daily SPY bars for the same window."""
    j = _get(f"{ALPACA_DATA}/v2/stocks/SPY/bars",
             {"timeframe": "1Day", "start": start_iso, "end": end_iso, "feed": "iex", "limit": 1000})
    return ((j or {}).get("bars") or [])


def _fetch_account() -> dict:
    return _get(f"{ALPACA_BROKER}/v2/account") or {}


def _fetch_positions() -> list[dict]:
    return _get(f"{ALPACA_BROKER}/v2/positions") or []


def _fetch_news(symbols: list[str]) -> list[dict]:
    params = {"limit": 30, "sort": "desc"}
    if symbols:
        params["symbols"] = ",".join(symbols)
    j = _get(f"{ALPACA_DATA}/v1beta1/news", params)
    return ((j or {}).get("news") or [])[:15]


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(INDEX))


@app.get("/backtest")
def backtest_page() -> FileResponse:
    return FileResponse(str(ROOT / "backtest.html"))


@app.get("/theme.css")
def theme_css() -> FileResponse:
    return FileResponse(str(ROOT / "theme.css"), media_type="text/css")


@app.get("/stocks")
def stocks_page() -> FileResponse:
    return FileResponse(str(ROOT / "stocks.html"))


# ===========================================================================
# Stock research endpoints
# ===========================================================================
from datetime import datetime, timedelta, timezone


_KNOWN_TICKERS = [
    {"sym": "AAPL", "name": "Apple Inc."},
    {"sym": "MSFT", "name": "Microsoft Corp."},
    {"sym": "GOOGL", "name": "Alphabet Inc."},
    {"sym": "NVDA", "name": "NVIDIA Corp."},
    {"sym": "TSLA", "name": "Tesla Inc."},
    {"sym": "XOM", "name": "ExxonMobil Corp."},
    {"sym": "CVX", "name": "Chevron Corp."},
    {"sym": "NEM", "name": "Newmont Corp."},
    {"sym": "GOLD", "name": "Barrick Gold"},
    {"sym": "OXY", "name": "Occidental Petroleum"},
    {"sym": "FCX", "name": "Freeport-McMoRan"},
    {"sym": "ALB", "name": "Albemarle Corp."},
    {"sym": "META", "name": "Meta Platforms"},
    {"sym": "AMZN", "name": "Amazon.com"},
    {"sym": "JPM", "name": "JPMorgan Chase"},
    {"sym": "V", "name": "Visa Inc."},
]


def _alpaca_latest_quote(ticker: str) -> dict:
    """Get the latest IEX quote for a ticker."""
    j = _get(f"{ALPACA_DATA}/v2/stocks/{ticker}/quotes/latest", {"feed": "iex"})
    return ((j or {}).get("quote") or {})


def _alpaca_latest_trade(ticker: str) -> dict:
    j = _get(f"{ALPACA_DATA}/v2/stocks/{ticker}/trades/latest", {"feed": "iex"})
    return ((j or {}).get("trade") or {})


@app.get("/api/tickers")
def list_tickers() -> JSONResponse:
    return JSONResponse(_KNOWN_TICKERS)


@app.get("/api/market/indices")
def market_indices() -> JSONResponse:
    """Major indices for the top ticker bar — uses Alpaca IEX feed."""
    # Use ETF proxies since Alpaca IEX doesn't cover indices directly
    indices = [
        ("SPY", "S&P 500"),
        ("QQQ", "Nasdaq 100"),
        ("DIA", "Dow 30"),
        ("IWM", "Russell 2000"),
        ("GLD", "Gold"),
        ("USO", "Oil"),
        ("TLT", "20Y Treasury"),
    ]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=10)
    out = []
    for sym, name in indices:
        # Get latest trade for current price
        trade = _alpaca_latest_trade(sym)
        bars_j = _get(
            f"{ALPACA_DATA}/v2/stocks/{sym}/bars",
            {
                "timeframe": "1Day",
                "start": start.strftime("%Y-%m-%d"),
                "end": end.strftime("%Y-%m-%d"),
                "feed": "iex",
                "limit": 10,
                "adjustment": "raw",
            },
        )
        bars = (bars_j or {}).get("bars") or []
        prev_close = bars[-2]["c"] if len(bars) >= 2 else None
        last = trade.get("p") or (bars[-1]["c"] if bars else None)
        chg = (last - prev_close) if (last and prev_close) else None
        chg_pct = (chg / prev_close) if (chg is not None and prev_close) else None
        out.append({
            "symbol": sym,
            "name": name,
            "price": last,
            "change": chg,
            "change_pct": chg_pct,
            "spark": [b["c"] for b in bars],
        })
    return JSONResponse(out)


@app.get("/api/stock/{ticker}")
def stock_detail(ticker: str, range: str = "3M") -> JSONResponse:
    """Full detail for one ticker: price history, key metrics, news."""
    ticker = ticker.upper()
    # Determine date range
    end = datetime.now(timezone.utc)
    days = {"1M": 30, "3M": 90, "6M": 180, "1Y": 365, "5Y": 1825, "ALL": 3650}.get(range.upper(), 90)
    start = end - timedelta(days=days)
    start_str, end_str = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    # Route through api.py so it uses the configured DATA_PROVIDER
    from src.tools.api import (
        get_prices,
        get_financial_metrics,
        get_company_news,
        get_market_cap,
    )

    try:
        prices = get_prices(ticker, start_str, end_str)
    except Exception as e:
        logger.warning("get_prices(%s) failed: %s", ticker, e)
        prices = []
    try:
        metrics_list = get_financial_metrics(ticker, end_str, limit=1)
        metrics = metrics_list[0] if metrics_list else None
    except Exception as e:
        logger.warning("get_financial_metrics(%s) failed: %s", ticker, e)
        metrics = None
    try:
        news = get_company_news(ticker, end_str, start_date=start_str, limit=20)
    except Exception as e:
        logger.warning("get_company_news(%s) failed: %s", ticker, e)
        news = []
    try:
        market_cap = get_market_cap(ticker, end_str)
    except Exception as e:
        market_cap = None

    # Latest live quote from Alpaca
    latest_trade = _alpaca_latest_trade(ticker)

    # 52-week stats from price history
    series_prices = [p.close for p in prices] if prices else []
    high_52w = max(series_prices) if series_prices else None
    low_52w = min(series_prices) if series_prices else None
    prev_close = series_prices[-2] if len(series_prices) >= 2 else None
    last_close = latest_trade.get("p") or (series_prices[-1] if series_prices else None)
    day_chg = (last_close - prev_close) if (last_close and prev_close) else None
    day_chg_pct = (day_chg / prev_close) if (day_chg is not None and prev_close) else None

    return JSONResponse({
        "ticker": ticker,
        "name": next((t["name"] for t in _KNOWN_TICKERS if t["sym"] == ticker), ticker),
        "range": range,
        "latest": {
            "price": last_close,
            "prev_close": prev_close,
            "change": day_chg,
            "change_pct": day_chg_pct,
            "latest_trade_time": latest_trade.get("t"),
            "ask": latest_trade.get("p"),
        },
        "stats": {
            "high_52w": high_52w,
            "low_52w": low_52w,
            "volume_avg": int(sum((p.volume or 0) for p in prices) / len(prices)) if prices else None,
            "market_cap": market_cap,
            "pe_ratio": getattr(metrics, "price_to_earnings_ratio", None) if metrics else None,
            "pb_ratio": getattr(metrics, "price_to_book_ratio", None) if metrics else None,
            "ps_ratio": getattr(metrics, "price_to_sales_ratio", None) if metrics else None,
            "peg_ratio": getattr(metrics, "peg_ratio", None) if metrics else None,
            "dividend_payout": getattr(metrics, "payout_ratio", None) if metrics else None,
            "eps": getattr(metrics, "earnings_per_share", None) if metrics else None,
            "roe": getattr(metrics, "return_on_equity", None) if metrics else None,
            "roa": getattr(metrics, "return_on_assets", None) if metrics else None,
            "gross_margin": getattr(metrics, "gross_margin", None) if metrics else None,
            "operating_margin": getattr(metrics, "operating_margin", None) if metrics else None,
            "net_margin": getattr(metrics, "net_margin", None) if metrics else None,
            "debt_to_equity": getattr(metrics, "debt_to_equity", None) if metrics else None,
            "current_ratio": getattr(metrics, "current_ratio", None) if metrics else None,
            "revenue_growth": getattr(metrics, "revenue_growth", None) if metrics else None,
        },
        "prices": [
            {"t": p.time, "o": p.open, "h": p.high, "l": p.low, "c": p.close, "v": p.volume}
            for p in prices
        ],
        "news": [
            {"title": n.title, "source": n.source, "date": n.date, "url": n.url}
            for n in news[:15]
        ],
    })


# ---------------------------------------------------------------------------
# Backtest log parser
# ---------------------------------------------------------------------------
import re
from datetime import datetime

_BT_LOG = Path(os.environ.get(
    "BACKTEST_LOG",
    str(Path(__file__).resolve().parents[2] / "logs" / "backtest_commodities.log"),
))
_DATE_RE = re.compile(r"\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*([A-Z0-9=.\-]{1,8})\s*\|\s*(\w+)\s*\|\s*(-?\d+)\s*\|\s*([\d.]+)\s*\|\s*(-?\d+)\s*\|\s*(-?\d+)\s*\|\s*(-?[\d,.]+)\s*\|")
_CASH_RE = re.compile(r"Cash Balance:\s*\$?(-?[\d,.]+)")
_POS_RE = re.compile(r"Total Position Value:\s*\$?(-?[\d,.]+)")
_SHARPE_RE = re.compile(r"Sharpe Ratio:\s*(-?[\d.]+)")
_DD_RE = re.compile(r"Max Drawdown:\s*(-?[\d.]+)%")


def _parse_backtest_log() -> dict:
    """Parse the qwen3 backtest log into structured data for the UI."""
    if not _BT_LOG.exists():
        return {"status": "no_log", "trades": [], "daily": [], "metrics": {}}

    text = _BT_LOG.read_text(errors="ignore")
    lines = text.splitlines()

    # Trades: list of dicts with date, ticker, action, qty, price, position_value
    trades: list[dict] = []
    for line in lines:
        m = _DATE_RE.search(line)
        if not m:
            continue
        date, ticker, action, qty, price, longs, shorts, pos_val = m.groups()
        try:
            trades.append({
                "date": date,
                "ticker": ticker,
                "action": action.strip().upper(),
                "qty": int(qty),
                "price": float(price),
                "long_shares": int(longs),
                "short_shares": int(shorts),
                "position_value": float(pos_val.replace(",", "")),
            })
        except ValueError:
            continue

    # Group trades by date to build daily equity series
    by_date: dict[str, list[dict]] = {}
    for t in trades:
        by_date.setdefault(t["date"], []).append(t)

    # Daily portfolio: total position value per day (sum across tickers) + initial cash inferred
    # Better: walk the cash balance lines in order; each "PORTFOLIO SUMMARY" block follows a date's trades.
    daily: list[dict] = []
    summaries = []  # (cash, pos_val) tuples in order
    i = 0
    while i < len(lines):
        if "PORTFOLIO SUMMARY" in lines[i]:
            # next ~6 lines have the summary fields
            block = "\n".join(lines[i : i + 8])
            cash_m = _CASH_RE.search(block)
            pos_m = _POS_RE.search(block)
            sharpe_m = _SHARPE_RE.search(block)
            dd_m = _DD_RE.search(block)
            if cash_m and pos_m:
                summaries.append({
                    "cash": float(cash_m.group(1).replace(",", "")),
                    "position_value": float(pos_m.group(1).replace(",", "")),
                    "sharpe": float(sharpe_m.group(1)) if sharpe_m else None,
                    "drawdown_pct": float(dd_m.group(1)) if dd_m else None,
                })
            i += 8
        else:
            i += 1

    # Pair each summary with the most recent date below it in the log (best-effort).
    # Since the log prints day-by-day in reverse order in the tables, we use the
    # chronological order of unique dates we extracted, aligned to summaries.
    unique_dates = sorted(set(t["date"] for t in trades))
    for idx, s in enumerate(summaries):
        if idx < len(unique_dates):
            s["date"] = unique_dates[idx]
            s["total_value"] = s["cash"] + s["position_value"]
            daily.append(s)

    last_metrics = summaries[-1] if summaries else {}
    progress_pct = round(100 * len(daily) / 44, 1) if daily else 0

    return {
        "status": "running" if len(daily) < 44 else "complete",
        "log_path": str(_BT_LOG),
        "log_size_bytes": _BT_LOG.stat().st_size,
        "log_mtime": datetime.fromtimestamp(_BT_LOG.stat().st_mtime).isoformat(),
        "trades": trades[:200],  # cap UI payload
        "trade_count": len(trades),
        "daily": daily,
        "days_complete": len(daily),
        "days_total": 44,
        "progress_pct": progress_pct,
        "metrics": {
            "cash_balance": last_metrics.get("cash"),
            "position_value": last_metrics.get("position_value"),
            "total_value": last_metrics.get("cash", 0) + last_metrics.get("position_value", 0) if last_metrics else None,
            "sharpe_ratio": last_metrics.get("sharpe"),
            "max_drawdown_pct": last_metrics.get("drawdown_pct"),
            "return_pct": ((last_metrics.get("cash", 0) + last_metrics.get("position_value", 0)) - 10000) / 10000 * 100 if last_metrics else None,
        },
    }


@app.get("/api/backtest")
def backtest_state() -> JSONResponse:
    return JSONResponse(_parse_backtest_log())


@app.get("/api/state")
def state() -> JSONResponse:
    account = _fetch_account()
    positions = _fetch_positions()
    history = _fetch_portfolio_history()

    timestamps = history.get("timestamp") or []
    equity = history.get("equity") or []

    # Build the SPY benchmark series for the same window
    spy_series = []
    if timestamps:
        from datetime import datetime, timezone
        start_iso = datetime.fromtimestamp(timestamps[0], tz=timezone.utc).strftime("%Y-%m-%d")
        end_iso = datetime.fromtimestamp(timestamps[-1], tz=timezone.utc).strftime("%Y-%m-%d")
        bars = _fetch_spy_history(start_iso, end_iso)
        spy_series = [{"t": b["t"], "c": b["c"]} for b in bars]

    symbols = [p["symbol"] for p in positions]
    news = _fetch_news(symbols)

    return JSONResponse({
        "account": {
            "cash": float(account.get("cash", 0) or 0),
            "equity": float(account.get("equity", 0) or 0),
            "last_equity": float(account.get("last_equity", 0) or 0),
            "buying_power": float(account.get("buying_power", 0) or 0),
            "status": account.get("status"),
        },
        "positions": [
            {
                "symbol": p["symbol"],
                "qty": float(p["qty"]),
                "side": p["side"],
                "avg_entry_price": float(p["avg_entry_price"]),
                "current_price": float(p.get("current_price") or 0),
                "market_value": float(p.get("market_value") or 0),
                "unrealized_pl": float(p.get("unrealized_pl") or 0),
                "unrealized_plpc": float(p.get("unrealized_plpc") or 0),
            }
            for p in positions
        ],
        "history": {
            "timestamps": timestamps,
            "equity": equity,
            "base_value": history.get("base_value"),
        },
        "spy": spy_series,
        "news": [
            {
                "id": n.get("id"),
                "headline": n.get("headline"),
                "summary": n.get("summary"),
                "url": n.get("url"),
                "source": n.get("source"),
                "symbols": n.get("symbols") or [],
                "created_at": n.get("created_at"),
            }
            for n in news
        ],
    })


def main():
    import uvicorn
    port = int(os.environ.get("DASHBOARD_PORT", "8765"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
