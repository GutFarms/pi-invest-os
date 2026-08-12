from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from pi_invest.config import AppConfig, EnvSettings
from pi_invest.models import (
    AccountSnapshot,
    OrderRequest,
    OrderResult,
    Position,
    Side,
    utcnow,
)
from pi_invest.storage.db import Database


class Broker(ABC):
    @abstractmethod
    def get_account(self, marks: dict[str, float] | None = None) -> AccountSnapshot: ...

    @abstractmethod
    def place_order(self, order: OrderRequest, mark_price: float) -> OrderResult: ...

    @abstractmethod
    def reset(self) -> None: ...


class PaperBroker(Broker):
    """Local double-entry style paper ledger persisted in SQLite."""

    def __init__(self, db: Database, starting_cash: float = 10_000.0) -> None:
        self.db = db
        self.starting_cash = starting_cash
        self.db.ensure_paper_account(starting_cash)

    def reset(self) -> None:
        self.db.reset_paper(self.starting_cash)

    def get_account(self, marks: dict[str, float] | None = None) -> AccountSnapshot:
        cash, positions_raw, day_start_equity = self.db.load_paper_state()
        positions: list[Position] = []
        equity = cash
        for sym, qty, avg in positions_raw:
            px = (marks or {}).get(sym, avg)
            pos = Position(symbol=sym, qty=qty, avg_cost=avg, market_price=px)
            positions.append(pos)
            equity += pos.market_value
        day_pnl = equity - day_start_equity
        day_pnl_pct = day_pnl / day_start_equity if day_start_equity else 0.0
        return AccountSnapshot(
            cash=cash,
            equity=equity,
            buying_power=cash,
            positions=positions,
            day_pnl=day_pnl,
            day_pnl_pct=day_pnl_pct,
        )

    def place_order(self, order: OrderRequest, mark_price: float) -> OrderResult:
        if order.side == Side.HOLD:
            return OrderResult(
                ok=False,
                symbol=order.symbol,
                side=order.side,
                message="hold — no order",
                paper=True,
            )
        if mark_price <= 0:
            return OrderResult(
                ok=False,
                symbol=order.symbol,
                side=order.side,
                message="invalid mark price",
                paper=True,
            )

        acct = self.get_account({order.symbol: mark_price})
        qty = order.qty
        if qty is None:
            notional = order.notional or 0.0
            qty = notional / mark_price

        if qty <= 0:
            return OrderResult(
                ok=False,
                symbol=order.symbol,
                side=order.side,
                message="qty must be positive",
                paper=True,
            )

        if order.side == Side.BUY:
            cost = qty * mark_price
            if cost > acct.cash + 1e-6:
                return OrderResult(
                    ok=False,
                    symbol=order.symbol,
                    side=order.side,
                    message=f"insufficient cash ({acct.cash:.2f} < {cost:.2f})",
                    paper=True,
                )
            self.db.apply_paper_fill(order.symbol, Side.BUY, qty, mark_price)
        else:
            held = next((p for p in acct.positions if p.symbol == order.symbol), None)
            if held is None or held.qty < qty - 1e-9:
                have = held.qty if held else 0.0
                return OrderResult(
                    ok=False,
                    symbol=order.symbol,
                    side=order.side,
                    message=f"insufficient shares ({have} < {qty})",
                    paper=True,
                )
            self.db.apply_paper_fill(order.symbol, Side.SELL, qty, mark_price)

        oid = str(uuid.uuid4())
        self.db.log_order(
            order_id=oid,
            symbol=order.symbol,
            side=order.side.value,
            qty=qty,
            fill_price=mark_price,
            paper=True,
            reason=order.reason,
            confidence=order.confidence,
        )
        return OrderResult(
            ok=True,
            order_id=oid,
            symbol=order.symbol,
            side=order.side,
            qty=qty,
            fill_price=mark_price,
            message="paper fill",
            paper=True,
            timestamp=utcnow(),
        )


class AlpacaBroker(Broker):
    """Minimal Alpaca Trading API v2 client via httpx."""

    def __init__(self, env: EnvSettings, paper: bool = True) -> None:
        import httpx

        self._httpx = httpx
        self.env = env
        self.paper = paper
        base = env.alpaca_base_url.rstrip("/")
        if not paper and "paper-api" in base:
            base = "https://api.alpaca.markets"
        self.base = base
        self.headers = {
            "APCA-API-KEY-ID": env.alpaca_api_key,
            "APCA-API-SECRET-KEY": env.alpaca_secret_key,
            "Content-Type": "application/json",
        }
        if not env.alpaca_api_key or not env.alpaca_secret_key:
            raise ValueError("Alpaca API keys are required for alpaca backend")

    def reset(self) -> None:
        raise NotImplementedError("Cannot reset a real Alpaca account from the agent")

    def _get(self, path: str) -> dict:
        with self._httpx.Client(timeout=30.0, headers=self.headers) as client:
            r = client.get(f"{self.base}{path}")
            r.raise_for_status()
            return r.json()

    def _post(self, path: str, json: dict) -> dict:
        with self._httpx.Client(timeout=30.0, headers=self.headers) as client:
            r = client.post(f"{self.base}{path}", json=json)
            r.raise_for_status()
            return r.json()

    def get_account(self, marks: dict[str, float] | None = None) -> AccountSnapshot:
        acct = self._get("/v2/account")
        positions_raw = self._get("/v2/positions")
        positions: list[Position] = []
        for p in positions_raw:
            positions.append(
                Position(
                    symbol=p["symbol"],
                    qty=float(p["qty"]),
                    avg_cost=float(p["avg_entry_price"]),
                    market_price=float(p["current_price"]),
                )
            )
        equity = float(acct["equity"])
        last_equity = float(acct.get("last_equity") or equity)
        day_pnl = equity - last_equity
        return AccountSnapshot(
            cash=float(acct["cash"]),
            equity=equity,
            buying_power=float(acct["buying_power"]),
            positions=positions,
            day_pnl=day_pnl,
            day_pnl_pct=day_pnl / last_equity if last_equity else 0.0,
        )

    def place_order(self, order: OrderRequest, mark_price: float) -> OrderResult:
        if order.side == Side.HOLD:
            return OrderResult(
                ok=False,
                symbol=order.symbol,
                side=order.side,
                message="hold",
                paper=self.paper,
            )
        body: dict = {
            "symbol": order.symbol.upper(),
            "side": order.side.value,
            "type": "market",
            "time_in_force": "day",
        }
        if order.notional and order.side == Side.BUY:
            body["notional"] = round(order.notional, 2)
        elif order.qty:
            body["qty"] = round(order.qty, 6)
        else:
            body["notional"] = round((order.notional or 0.0), 2)

        try:
            resp = self._post("/v2/orders", body)
        except Exception as exc:  # noqa: BLE001
            return OrderResult(
                ok=False,
                symbol=order.symbol,
                side=order.side,
                message=str(exc),
                paper=self.paper,
            )
        return OrderResult(
            ok=True,
            order_id=str(resp.get("id", "")),
            symbol=order.symbol,
            side=order.side,
            qty=float(resp.get("qty") or order.qty or 0),
            fill_price=mark_price,
            message=str(resp.get("status", "submitted")),
            paper=self.paper,
        )


def build_broker(cfg: AppConfig, env: EnvSettings, db: Database) -> Broker:
    live_requested = cfg.agent.mode == "live"
    if live_requested and not env.allow_live_trading:
        raise RuntimeError(
            "Live mode requested but ALLOW_LIVE_TRADING is not true. "
            "Refusing to start — keep mode=paper or unlock live trading explicitly."
        )
    if cfg.broker.backend == "paper":
        return PaperBroker(db, starting_cash=cfg.broker.starting_cash)
    return AlpacaBroker(env, paper=not live_requested)
