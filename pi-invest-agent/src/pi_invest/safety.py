from __future__ import annotations

from typing import TYPE_CHECKING

from pi_invest.models import HaltState, utcnow
from pi_invest.storage.db import Database

if TYPE_CHECKING:
    from pi_invest.alerts import AlertBus


class HaltedError(RuntimeError):
    """Raised when trading or outbound sends are frozen by the kill switch."""


class SafetyGate:
    """Persistent kill switch — blocks brokerage orders and wallet sends."""

    def __init__(self, db: Database, alerts: AlertBus | None = None) -> None:
        self.db = db
        self.alerts = alerts

    def state(self) -> HaltState:
        halted, reason, updated = self.db.get_halt_state()
        ts = None
        if updated:
            try:
                from datetime import datetime

                ts = datetime.fromisoformat(updated)
            except ValueError:
                ts = utcnow()
        return HaltState(halted=halted, reason=reason, updated_at=ts)

    def is_halted(self) -> bool:
        return self.state().halted

    def halt(self, reason: str = "manual halt") -> HaltState:
        reason = reason or "manual halt"
        self.db.set_halt_state(True, reason=reason)
        if self.alerts is not None:
            self.alerts.halt(reason)
        return self.state()

    def resume(self) -> HaltState:
        self.db.set_halt_state(False, reason="")
        if self.alerts is not None:
            self.alerts.resume()
        return self.state()

    def assert_trading_allowed(self) -> None:
        st = self.state()
        if st.halted:
            raise HaltedError(f"trading halted: {st.reason or 'kill switch active'}")

    def assert_send_allowed(self) -> None:
        st = self.state()
        if st.halted:
            raise HaltedError(f"sends halted: {st.reason or 'kill switch active'}")
