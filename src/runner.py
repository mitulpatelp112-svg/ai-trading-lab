"""End-to-end runner: scan → analyze → execute on Alpaca.

Two modes:
  python -m src.runner once            # single scan+analyze+execute cycle
  python -m src.runner loop            # 30-min cadence + news-watcher trigger

Required env vars:
  ANTHROPIC_API_KEY
  MASSIVE_API_KEY
  ALPACA_API_KEY, ALPACA_SECRET_KEY
  DATA_PROVIDER=massive
  EXECUTION_MODE=dry-run|paper|live   (default dry-run)
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage

logger = logging.getLogger("runner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# Default starter persona set (cheaper). Override with PERSONAS=comma,separated,keys
DEFAULT_PERSONAS = [
    "warren_buffett",
    "michael_burry",
    "peter_lynch",
    "stanley_druckenmiller",
    "cathie_wood",
]

# Quant agents always run (no LLM cost)
QUANT_AGENTS = ["technical_analyst", "fundamentals_analyst", "sentiment_analyst", "valuation_analyst"]

LOG_DIR = Path(os.environ.get("RUNNER_LOG_DIR", "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _selected_analysts() -> list[str]:
    override = os.environ.get("PERSONAS")
    if override:
        return [s.strip() for s in override.split(",") if s.strip()]
    return DEFAULT_PERSONAS + QUANT_AGENTS


def _log_run(payload: dict, tag: str) -> None:
    fname = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{tag}.json"
    (LOG_DIR / fname).write_text(json.dumps(payload, indent=2, default=str))
    logger.info("Wrote run log: %s", LOG_DIR / fname)


def run_once(trigger: str = "scheduled") -> dict:
    """One full scan → analyze → execute cycle."""
    from src.main import create_workflow
    from src.scanner.universe import scan_universe
    from src.execution.alpaca_executor import (
        AlpacaConfig, AlpacaClient, build_portfolio_from_alpaca,
        check_circuit_breaker, execute_decisions,
    )

    cfg = AlpacaConfig()
    client = AlpacaClient(cfg)

    halted, reason = check_circuit_breaker(client, cfg)
    if halted:
        logger.warning("Circuit breaker active: %s — skipping run", reason)
        return {"halted": True, "reason": reason}

    logger.info("Scanning universe...")
    picks = scan_universe(top_n=int(os.environ.get("UNIVERSE_SIZE", "10")))
    tickers = [p["ticker"] for p in picks]
    if not tickers:
        logger.warning("Universe scan returned empty")
        return {"halted": False, "tickers": [], "reason": "empty_universe"}
    logger.info("Tickers selected: %s", tickers)

    portfolio = build_portfolio_from_alpaca(client, tickers)
    logger.info("Portfolio: cash=$%.2f equity=$%.2f", portfolio["cash"], portfolio.get("_total_equity", 0))

    selected = _selected_analysts()
    workflow = create_workflow(selected).compile()

    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_date = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")

    model_name = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
    state = {
        "messages": [HumanMessage(content="Make trading decisions based on the provided data.")],
        "data": {
            "tickers": tickers,
            "portfolio": portfolio,
            "start_date": start_date,
            "end_date": end_date,
            "analyst_signals": {},
        },
        "metadata": {
            "show_reasoning": False,
            "model_name": model_name,
            "model_provider": "Anthropic",
        },
    }

    logger.info("Running analyst graph with %d agents...", len(selected))
    final_state = workflow.invoke(state)

    # Last message from portfolio_manager is JSON of decisions
    last_msg = final_state["messages"][-1]
    try:
        decisions = json.loads(last_msg.content)
    except (TypeError, json.JSONDecodeError) as e:
        logger.error("Failed to parse portfolio_manager output: %s", e)
        return {"halted": False, "tickers": tickers, "decisions": None, "error": str(e)}

    logger.info("Decisions: %s", decisions)

    exec_records = execute_decisions(decisions, cfg)

    payload = {
        "trigger": trigger,
        "ts": datetime.utcnow().isoformat(),
        "tickers": tickers,
        "picks": picks,
        "decisions": decisions,
        "executions": exec_records,
        "mode": cfg.mode,
    }
    _log_run(payload, trigger)
    return payload


def _market_open_now() -> bool:
    """Cheap NYSE open check (Mon-Fri 13:30-20:00 UTC). Doesn't account for holidays."""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 13 * 60 + 30 <= minutes < 20 * 60


def _watch_news_loop(seen: set[str], interval_seconds: int = 60) -> str | None:
    """Poll Massive news. Return a ticker if a high-impact unseen headline appears."""
    from src.tools.massive_api import _request
    since = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = _request("/v2/reference/news", {
        "published_utc.gte": since,
        "order": "desc",
        "sort": "published_utc",
        "limit": 50,
    })
    if not data:
        return None
    for article in data.get("results") or []:
        aid = article.get("id")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        # Trigger if any insight has strong sentiment
        for insight in article.get("insights") or []:
            sentiment = (insight.get("sentiment") or "").lower()
            ticker = insight.get("ticker")
            if sentiment in {"positive", "negative"} and ticker:
                logger.info("News trigger: %s (%s) — %s", ticker, sentiment, article.get("title"))
                return ticker
    return None


def run_loop() -> None:
    """Loop: every 30 min during market hours + news-triggered runs."""
    seen_articles: set[str] = set()
    last_scheduled = 0.0
    while True:
        try:
            if not _market_open_now():
                logger.info("Market closed — sleeping 5 min")
                time.sleep(300)
                continue

            now = time.time()
            if now - last_scheduled >= 1800:  # 30 minutes
                logger.info("=== 30-min scheduled run ===")
                run_once(trigger="scheduled")
                last_scheduled = now

            # News watcher (every minute)
            trigger_ticker = _watch_news_loop(seen_articles)
            if trigger_ticker:
                logger.info("=== News-triggered run (%s) ===", trigger_ticker)
                run_once(trigger=f"news_{trigger_ticker}")
                last_scheduled = time.time()  # reset so we don't immediately re-fire

            time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Stopped by user")
            return
        except Exception as e:
            logger.exception("Loop iteration failed: %s", e)
            time.sleep(60)


def main():
    load_dotenv(override=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["once", "loop", "scan"], default="once", nargs="?")
    args = parser.parse_args()

    if args.command == "scan":
        from src.scanner.universe import scan_universe
        print(json.dumps(scan_universe(top_n=int(os.environ.get("UNIVERSE_SIZE", "10"))), indent=2))
    elif args.command == "once":
        run_once(trigger="manual")
    elif args.command == "loop":
        run_loop()


if __name__ == "__main__":
    main()
