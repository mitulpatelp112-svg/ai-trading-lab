"""Universe scanner — picks the day's top N tickers worth analyzing.

Primary source: Alpaca's free screener (works on paper accounts):
  /v1beta1/screener/stocks/most-actives  - highest-volume names
  /v1beta1/screener/stocks/movers        - top gainers & losers
  /v1beta1/news                          - market news

Optional augmentation: Massive's /v2/reference/news endpoint adds
ticker-level sentiment scores when MASSIVE_API_KEY is present.

We then filter out ETFs, leveraged/inverse products, and penny stocks
(price < $5) so the persona agents work on real operating companies.
"""
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

ALPACA_DATA_URL = "https://data.alpaca.markets"

# ETFs / leveraged / inverse / volatility products we don't want personas analyzing.
_EXCLUDE = {
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "VUG", "VTV", "BND", "AGG",
    "TQQQ", "SQQQ", "SOXL", "SOXS", "TZA", "TNA", "FAS", "FAZ",
    "UVXY", "VXX", "SVXY", "UVIX", "SVIX", "HAO",
    "TLT", "GLD", "SLV", "USO", "ARKK", "XLF", "XLE", "XLK", "XLV",
}

MIN_PRICE = float(os.environ.get("SCANNER_MIN_PRICE", "5"))


def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", "").strip(),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", "").strip(),
    }


def _alpaca_get(path: str, params: dict | None = None) -> dict | None:
    try:
        r = requests.get(f"{ALPACA_DATA_URL}{path}", headers=_alpaca_headers(), params=params or {}, timeout=20)
    except requests.RequestException as e:
        logger.warning("Alpaca request error %s: %s", path, e)
        return None
    if r.status_code != 200:
        logger.warning("Alpaca %s returned %s: %s", path, r.status_code, r.text[:200])
        return None
    return r.json()


def _ok(ticker: str) -> bool:
    if not ticker or ticker.upper() in _EXCLUDE:
        return False
    if any(ch in ticker for ch in ".-=+"):
        return False
    return ticker.isalpha() and 1 <= len(ticker) <= 5


def _fetch_most_actives(top: int = 30) -> list[dict]:
    data = _alpaca_get("/v1beta1/screener/stocks/most-actives", {"by": "volume", "top": top})
    return (data or {}).get("most_actives") or []


def _fetch_movers(top: int = 25) -> tuple[list[dict], list[dict]]:
    data = _alpaca_get("/v1beta1/screener/stocks/movers", {"top": top})
    if not data:
        return [], []
    return data.get("gainers") or [], data.get("losers") or []


def _fetch_alpaca_news(symbols: list[str] | None = None, limit: int = 50) -> list[dict]:
    params: dict = {"limit": limit, "sort": "desc"}
    if symbols:
        params["symbols"] = ",".join(symbols)
    data = _alpaca_get("/v1beta1/news", params)
    return (data or {}).get("news") or []


def _fetch_massive_news_sentiment(lookback_hours: int = 24, limit: int = 200) -> dict[str, dict]:
    """Return {ticker: {sentiment, headline}} from Massive's news insights (free tier).

    Silently returns {} if Massive key missing or call fails.
    """
    key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if not key:
        return {}
    since = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        r = requests.get(
            "https://api.massive.com/v2/reference/news",
            headers={"Authorization": f"Bearer {key}"},
            params={"published_utc.gte": since, "order": "desc", "sort": "published_utc", "limit": limit},
            timeout=20,
        )
    except requests.RequestException:
        return {}
    if r.status_code != 200:
        return {}
    out: dict[str, dict] = {}
    for art in (r.json().get("results") or []):
        for insight in art.get("insights") or []:
            t = insight.get("ticker")
            sent = (insight.get("sentiment") or "").lower()
            if t and sent in {"positive", "negative"} and t not in out:
                out[t] = {"sentiment": sent, "headline": art.get("title")}
    return out


def scan_universe(top_n: int = 10) -> list[dict]:
    """Return up to top_n tickers ranked by combined signal.

    Score sources:
      - Top gainers (weight 3, rank-weighted)
      - Top losers  (weight 3, rank-weighted)
      - Most-active by volume (weight 2.5, rank-weighted)
      - Massive news with strong sentiment (weight 1.5 bonus)
    """
    scores: dict[str, float] = defaultdict(float)
    meta: dict[str, dict] = defaultdict(lambda: {"sources": set(), "change_pct": None, "price": None, "volume": None, "headline": None})

    # 1) Gainers + losers
    gainers, losers = _fetch_movers(top=25)
    for direction, rows in (("gainers", gainers), ("losers", losers)):
        for rank, row in enumerate(rows):
            t = row.get("symbol")
            if not _ok(t):
                continue
            price = float(row.get("price") or 0)
            if price < MIN_PRICE:
                continue
            scores[t] += 3.0 * (25 - rank) / 25.0
            meta[t]["sources"].add(direction)
            meta[t]["change_pct"] = row.get("percent_change")
            meta[t]["price"] = price

    # 2) Most-actives
    actives = _fetch_most_actives(top=50)
    for rank, row in enumerate(actives):
        t = row.get("symbol")
        if not _ok(t):
            continue
        scores[t] += 2.5 * (50 - rank) / 50.0
        meta[t]["sources"].add("most_active")
        if meta[t]["volume"] is None:
            meta[t]["volume"] = row.get("volume")

    # 3) Massive news sentiment bonus (free tier — optional)
    sentiment_map = _fetch_massive_news_sentiment()
    for t, info in sentiment_map.items():
        if t in scores:  # only boost names already in our candidate pool
            scores[t] += 1.5
            meta[t]["sources"].add("news")
            meta[t]["headline"] = info["headline"]

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    result = []
    for ticker, score in ranked:
        m = meta[ticker]
        result.append({
            "ticker": ticker,
            "score": round(score, 3),
            "sources": sorted(m["sources"]),
            "change_pct": m["change_pct"],
            "price": m["price"],
            "volume": m["volume"],
            "headline": m["headline"],
        })
        if len(result) >= top_n:
            break
    return result


if __name__ == "__main__":
    import json
    from dotenv import load_dotenv
    load_dotenv(override=True)
    picks = scan_universe(top_n=int(os.environ.get("UNIVERSE_SIZE", "10")))
    print(json.dumps(picks, indent=2))
