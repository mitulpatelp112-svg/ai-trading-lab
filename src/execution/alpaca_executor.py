"""Alpaca brokerage adapter.

Reads live portfolio state and executes the portfolio_manager's decisions.

Three execution modes (env var EXECUTION_MODE):
  - dry-run  : log intended orders, place nothing (default; safest)
  - paper    : route orders to Alpaca paper account
  - live     : route to live Alpaca account (requires extra confirmation env var)

Portfolio state is fetched from Alpaca and adapted into the dict shape the
existing portfolio_manager expects.
"""
import logging
import os
from datetime import datetime
from typing import Any

import requests

logger = logging.getLogger(__name__)

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


class AlpacaConfig:
    def __init__(self) -> None:
        self.api_key = os.environ.get("ALPACA_API_KEY", "")
        self.secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
        self.mode = (os.environ.get("EXECUTION_MODE") or "dry-run").lower()
        self.live_confirmed = os.environ.get("I_UNDERSTAND_THIS_IS_REAL_MONEY") == "yes"
        # Per-trade safety cap (in dollars). Default $1,000 per order on a $10k book.
        self.max_order_notional = float(os.environ.get("MAX_ORDER_NOTIONAL", "1000"))
        # Hard daily loss circuit breaker (as a fraction of equity).
        self.daily_loss_halt_pct = float(os.environ.get("DAILY_LOSS_HALT_PCT", "0.05"))

    @property
    def base_url(self) -> str:
        if self.mode == "live":
            if not self.live_confirmed:
                raise RuntimeError(
                    "EXECUTION_MODE=live requires I_UNDERSTAND_THIS_IS_REAL_MONEY=yes"
                )
            return LIVE_URL
        return PAPER_URL

    def headers(self) -> dict:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
        }


class AlpacaClient:
    def __init__(self, cfg: AlpacaConfig | None = None) -> None:
        self.cfg = cfg or AlpacaConfig()

    def _req(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.cfg.base_url}{path}"
        resp = requests.request(method, url, headers=self.cfg.headers(), timeout=30, **kwargs)
        if resp.status_code >= 400:
            logger.warning("Alpaca %s %s -> %s: %s", method, path, resp.status_code, resp.text[:200])
            return None
        return resp.json()

    def get_account(self) -> dict | None:
        return self._req("GET", "/v2/account")

    def get_positions(self) -> list[dict]:
        return self._req("GET", "/v2/positions") or []

    def submit_order(self, ticker: str, qty: int, side: str, order_type: str = "market", tif: str = "day") -> dict | None:
        body = {
            "symbol": ticker,
            "qty": str(qty),
            "side": side,
            "type": order_type,
            "time_in_force": tif,
        }
        return self._req("POST", "/v2/orders", json=body)

    def close_position(self, ticker: str, qty: int | None = None) -> dict | None:
        path = f"/v2/positions/{ticker}"
        if qty is not None:
            path += f"?qty={qty}"
        return self._req("DELETE", path)

    def latest_trade_price(self, ticker: str) -> float | None:
        """Latest IEX trade price for any ticker (free with paper account)."""
        try:
            resp = requests.get(
                f"https://data.alpaca.markets/v2/stocks/{ticker}/trades/latest",
                headers=self.cfg.headers(),
                params={"feed": "iex"},
                timeout=15,
            )
        except requests.RequestException:
            return None
        if resp.status_code != 200:
            return None
        trade = (resp.json() or {}).get("trade") or {}
        price = trade.get("p")
        return float(price) if price else None


def build_portfolio_from_alpaca(client: AlpacaClient, tickers: list[str]) -> dict:
    """Translate Alpaca state into the dict shape portfolio_manager expects."""
    account = client.get_account() or {}
    positions = client.get_positions()

    cash = float(account.get("cash", 0))
    portfolio_value = float(account.get("portfolio_value", 0))

    positions_by_ticker = {p["symbol"]: p for p in positions}

    portfolio: dict[str, Any] = {
        "cash": cash,
        "margin_requirement": 0.0,
        "margin_used": 0.0,
        "positions": {},
        "realized_gains": {},
    }
    for t in tickers:
        p = positions_by_ticker.get(t)
        if p is None:
            portfolio["positions"][t] = {
                "long": 0, "short": 0, "long_cost_basis": 0.0, "short_cost_basis": 0.0,
                "short_margin_used": 0.0,
            }
        else:
            qty = int(float(p["qty"]))
            avg = float(p["avg_entry_price"])
            if qty >= 0:
                portfolio["positions"][t] = {
                    "long": qty, "short": 0, "long_cost_basis": avg, "short_cost_basis": 0.0,
                    "short_margin_used": 0.0,
                }
            else:
                portfolio["positions"][t] = {
                    "long": 0, "short": abs(qty), "long_cost_basis": 0.0, "short_cost_basis": avg,
                    "short_margin_used": 0.0,
                }
        portfolio["realized_gains"][t] = {"long": 0.0, "short": 0.0}

    portfolio["_total_equity"] = portfolio_value
    return portfolio


def execute_decisions(decisions: dict[str, dict], cfg: AlpacaConfig | None = None) -> list[dict]:
    """Translate portfolio_manager decisions into Alpaca orders.

    decisions[ticker] = {action: buy|sell|short|cover|hold, quantity: int, ...}
    Returns a list of execution records (one per ticker).
    """
    cfg = cfg or AlpacaConfig()
    client = AlpacaClient(cfg)

    # Resolve a price for each ticker to enforce notional caps.
    positions = {p["symbol"]: p for p in client.get_positions()}

    records: list[dict] = []
    for ticker, dec in decisions.items():
        action = dec.get("action")
        qty = int(dec.get("quantity", 0) or 0)
        record = {
            "ticker": ticker,
            "action": action,
            "requested_qty": qty,
            "submitted_qty": 0,
            "status": "skipped",
            "mode": cfg.mode,
            "ts": datetime.utcnow().isoformat(),
            "reason": dec.get("reasoning"),
        }
        if action == "hold" or qty <= 0:
            records.append(record)
            continue

        # Per-trade notional cap. Use existing position's current_price if held,
        # otherwise fetch a real-time IEX trade price.
        ref_price = float((positions.get(ticker) or {}).get("current_price") or 0)
        if ref_price <= 0:
            ref_price = client.latest_trade_price(ticker) or 0
        if ref_price > 0:
            max_qty = int(cfg.max_order_notional // ref_price)
            if max_qty <= 0:
                record["status"] = "blocked_notional_cap"
                record["ref_price"] = ref_price
                records.append(record)
                continue
            if qty > max_qty:
                record["capped_from"] = qty
                qty = max_qty
            record["ref_price"] = ref_price
        else:
            record["ref_price"] = None  # Couldn't price the trade; let it through with original qty

        side_map = {"buy": "buy", "sell": "sell", "short": "sell", "cover": "buy"}
        side = side_map.get(action)
        if not side:
            record["status"] = "unknown_action"
            records.append(record)
            continue

        record["submitted_qty"] = qty
        if cfg.mode == "dry-run":
            record["status"] = "dry_run_logged"
            logger.info("[DRY-RUN] %s %s %s shares", side.upper(), ticker, qty)
            records.append(record)
            continue

        result = client.submit_order(ticker, qty, side)
        if result and result.get("id"):
            record["status"] = "submitted"
            record["order_id"] = result["id"]
        else:
            record["status"] = "failed"
        records.append(record)

    return records


def check_circuit_breaker(client: AlpacaClient, cfg: AlpacaConfig) -> tuple[bool, str]:
    """Return (halted, reason). True means stop trading for the day."""
    account = client.get_account()
    if not account:
        return False, ""
    equity = float(account.get("equity", 0))
    last_equity = float(account.get("last_equity", 0))
    if last_equity <= 0:
        return False, ""
    pnl_pct = (equity - last_equity) / last_equity
    if pnl_pct <= -cfg.daily_loss_halt_pct:
        return True, f"Daily P&L {pnl_pct:.2%} hit halt threshold {-cfg.daily_loss_halt_pct:.2%}"
    return False, ""
