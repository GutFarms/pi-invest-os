from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from typing import TYPE_CHECKING, Any

from pi_invest.agent.guard import InvestmentGuard
from pi_invest.agent.llm import LlmAdvisor
from pi_invest.agent.risk import RiskGate
from pi_invest.agent.scoring import heuristic_intents, score_symbol
from pi_invest.broker import Broker
from pi_invest.config import AppConfig, EnvSettings
from pi_invest.data import ResilientMarketData
from pi_invest.journal import PerformanceJournal
from pi_invest.models import Decision, Side, TradeIntent, utcnow
from pi_invest.safety import HaltedError, SafetyGate
from pi_invest.storage.db import Database
from pi_invest.wallet import WalletService

if TYPE_CHECKING:
    from pi_invest.alerts import AlertBus


def _market_open(tz_name: str) -> bool:
    try:
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:  # noqa: BLE001
        now = utcnow()
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


class InvestAgent:
    def __init__(
        self,
        cfg: AppConfig,
        env: EnvSettings,
        market: ResilientMarketData,
        broker: Broker,
        db: Database,
        safety: SafetyGate | None = None,
        journal: PerformanceJournal | None = None,
        wallet: WalletService | None = None,
        alerts: AlertBus | None = None,
    ) -> None:
        self.cfg = cfg
        self.env = env
        self.market = market
        self.broker = broker
        self.db = db
        self.risk = RiskGate(cfg.risk, cfg.universe)
        self.guard = InvestmentGuard(cfg.guard)
        self.llm = LlmAdvisor(cfg.llm, env)
        self.safety = safety or SafetyGate(db)
        self.journal = journal or PerformanceJournal(db, self.safety)
        self.wallet = wallet
        self.alerts = alerts

    def run_cycle(self, dry_run: bool = False) -> Decision:
        cycle_id = str(uuid.uuid4())
        skipped: list[str] = []
        market_open = _market_open(self.cfg.schedule.market_tz)

        quotes: dict[str, float] = {}
        scores = []
        for symbol in self.cfg.universe:
            try:
                hist = self.market.get_history(
                    symbol, lookback_days=self.cfg.market.lookback_days
                )
                quote = self.market.get_quote(symbol)
                quotes[symbol.upper()] = quote.price
                scores.append(score_symbol(quote, hist))
            except Exception as exc:  # noqa: BLE001
                skipped.append(f"{symbol}: data error ({exc})")

        marks = dict(quotes)
        account = self.broker.get_account(marks)
        try:
            self.db.rollover_day_start_if_needed(account.equity)
            account = self.broker.get_account(marks)
        except Exception:  # noqa: BLE001
            pass

        # Kill switch: still score/plan, never place orders
        halted = self.safety.is_halted()
        if halted:
            st = self.safety.state()
            skipped.append(f"HALTED: {st.reason or 'kill switch active'}")

        intents: list[TradeIntent] = self.risk.trim_overweight(account)
        heur = heuristic_intents(scores) if scores else []

        llm_raw = None
        guard_meta: dict = {}
        if self.llm.enabled() and scores:
            llm_intents, llm_raw = self.llm.advise(scores, account, self.cfg.universe)
            if llm_raw and str(llm_raw).startswith("llm error"):
                skipped.append(str(llm_raw))
                llm_intents = []
            merged, guard_notes, guard_meta = self.guard.merge_intents(
                scores, llm_intents or [], heur
            )
            intents.extend(merged)
            skipped.extend(guard_notes)
            self._audit_guard(cycle_id, guard_meta)
        else:
            merged, guard_notes, guard_meta = self.guard.merge_intents(
                scores, [], heur
            )
            intents.extend(merged)
            skipped.extend(guard_notes)

        # Drawdown throttle (Alinia-style capital protection)
        try:
            summary = self.journal.summary()
            dd = float(summary.max_drawdown_pct or 0.0)
        except Exception:  # noqa: BLE001
            dd = 0.0
        intents, dd_notes = self.guard.apply_drawdown_throttle(intents, dd)
        skipped.extend(dd_notes)

        held = {p.symbol for p in account.positions}
        filtered_intents: list[TradeIntent] = []
        for intent in intents:
            if intent.side == Side.SELL and intent.symbol not in held:
                continue
            filtered_intents.append(intent)

        if (
            self.cfg.schedule.prefer_market_hours
            and not market_open
            and self.cfg.agent.mode == "live"
        ):
            decision = Decision(
                cycle_id=cycle_id,
                market_open=market_open,
                scores=scores,
                intents=filtered_intents,
                skipped_reasons=skipped + ["live trading blocked outside hours"],
                llm_raw=llm_raw,
                account_after=account,
                meta={
                    "data_source": self.market.last_source,
                    "halted": halted,
                    "guard": guard_meta,
                    "drawdown_pct": dd,
                },
            )
            self.db.save_decision(decision)
            self._journal(account, cycle_id)
            return decision

        if self.cfg.schedule.prefer_market_hours and not market_open:
            skipped.append("outside preferred US market hours (paper still executes)")

        safe_intents, risk_notes = self.risk.filter_intents(filtered_intents, account)
        skipped.extend(risk_notes)

        orders = []
        planned_orders: list[dict[str, Any]] = []
        if dry_run:
            skipped.append("preview/dry-run: orders not sent")
            planned_orders = self._plan_orders(safe_intents, marks, skipped)
        elif halted:
            skipped.append("orders blocked by halt")
            planned_orders = self._plan_orders(safe_intents, marks, skipped)
        else:
            try:
                self.safety.assert_trading_allowed()
            except HaltedError as exc:
                skipped.append(str(exc))
                planned_orders = self._plan_orders(safe_intents, marks, skipped)
            else:
                for intent in safe_intents:
                    account = self.broker.get_account(marks)
                    px = marks.get(intent.symbol)
                    if px is None:
                        skipped.append(f"{intent.symbol}: missing mark")
                        continue
                    order_req, reason = self.risk.to_order(intent, account, px)
                    if order_req is None:
                        skipped.append(f"{intent.symbol}: {reason}")
                        continue
                    result = self.broker.place_order(order_req, px)
                    orders.append(result)

        account_after = self.broker.get_account(marks)
        decision = Decision(
            cycle_id=cycle_id,
            market_open=market_open,
            scores=sorted(scores, key=lambda s: s.expected_income_proxy, reverse=True),
            intents=safe_intents,
            orders=orders,
            skipped_reasons=skipped,
            llm_raw=llm_raw,
            account_after=account_after,
            meta={
                "data_source": self.market.last_source,
                "mode": self.cfg.agent.mode,
                "backend": self.cfg.broker.backend,
                "agent": self.cfg.agent.name,
                "halted": halted,
                "preview": dry_run,
                "planned_orders": planned_orders,
                "guard": guard_meta,
                "drawdown_pct": dd,
            },
        )
        self.db.save_decision(decision)
        if self.cfg.safety.journal_enabled:
            self._journal(account_after, cycle_id)
        return decision

    def _audit_guard(self, cycle_id: str, meta: dict) -> None:
        if not self.cfg.guard.audit_intents or not self.cfg.safety.audit_enabled:
            return
        try:
            blocked = meta.get("blocked") or []
            allowed = meta.get("allowed") or []
            if blocked:
                self.db.audit(
                    "intent.guard.blocked",
                    f"{cycle_id}: " + "; ".join(blocked[:12]),
                )
            if allowed:
                self.db.audit(
                    "intent.guard.allowed",
                    f"{cycle_id}: " + "; ".join(allowed[:12]),
                )
        except Exception:  # noqa: BLE001
            pass

    def _plan_orders(
        self,
        intents: list[TradeIntent],
        marks: dict[str, float],
        skipped: list[str],
    ) -> list[dict[str, Any]]:
        """Compute orders that would be placed without submitting them."""
        planned: list[dict[str, Any]] = []
        account = self.broker.get_account(marks)
        for intent in intents:
            px = marks.get(intent.symbol)
            if px is None:
                skipped.append(f"{intent.symbol}: missing mark (preview)")
                continue
            order_req, reason = self.risk.to_order(intent, account, px)
            if order_req is None:
                skipped.append(f"{intent.symbol}: {reason}")
                continue
            planned.append(order_req.model_dump(mode="json"))
        return planned

    def _journal(self, account, cycle_id: str) -> None:
        if not self.cfg.safety.journal_enabled:
            return
        wallet_snap = None
        if self.wallet is not None:
            try:
                wallet_snap = self.wallet.snapshot()
            except Exception:  # noqa: BLE001
                wallet_snap = None
        if (
            wallet_snap is not None
            and wallet_snap.backend == "coinbase-error"
            and self.alerts is not None
        ):
            try:
                self.alerts.coinbase_error(
                    str(wallet_snap.meta.get("error") or "Coinbase snapshot failed")
                )
            except Exception:  # noqa: BLE001
                pass
        try:
            self.journal.record(account, wallet_snap, cycle_id=cycle_id)
        except Exception:  # noqa: BLE001
            pass
