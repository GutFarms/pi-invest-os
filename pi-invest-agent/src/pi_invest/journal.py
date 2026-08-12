from __future__ import annotations

import csv
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from pi_invest.models import (
    AccountSnapshot,
    EquitySnapshot,
    JournalSummary,
    WalletSnapshot,
)
from pi_invest.safety import SafetyGate
from pi_invest.storage.db import Database

if TYPE_CHECKING:
    from pi_invest.alerts import AlertBus


class PerformanceJournal:
    """Tracks brokerage + wallet NAV, peak, and drawdown over time."""

    def __init__(
        self,
        db: Database,
        safety: SafetyGate,
        alerts: AlertBus | None = None,
    ) -> None:
        self.db = db
        self.safety = safety
        self.alerts = alerts

    def record(
        self,
        account: AccountSnapshot,
        wallet: WalletSnapshot | None = None,
        cycle_id: str | None = None,
    ) -> EquitySnapshot:
        wallet_usd = wallet.total_usd_estimate if wallet else 0.0
        total_nav = account.equity + wallet_usd
        prior_peak = self.db.peak_nav()
        peak = max(prior_peak, total_nav)
        drawdown = ((peak - total_nav) / peak) if peak > 0 else 0.0
        halt = self.safety.state()
        snap = EquitySnapshot(
            snapshot_id=str(uuid.uuid4()),
            equity=account.equity,
            cash=account.cash,
            wallet_usd=wallet_usd,
            total_nav=total_nav,
            day_pnl=account.day_pnl,
            day_pnl_pct=account.day_pnl_pct,
            peak_nav=peak,
            drawdown_pct=drawdown,
            halted=halt.halted,
            cycle_id=cycle_id,
        )
        self.db.save_equity_snapshot(snap)
        if self.alerts is not None:
            try:
                self.alerts.drawdown(snap.drawdown_pct, snap.total_nav, snap.peak_nav)
            except Exception:  # noqa: BLE001
                pass
        return snap

    def history(self, limit: int = 50) -> list[EquitySnapshot]:
        return self.db.list_equity_snapshots(limit=limit)

    def summary(self, limit: int = 500) -> JournalSummary:
        # list is newest-first; reverse for start→latest
        points = list(reversed(self.db.list_equity_snapshots(limit=limit)))
        halt = self.safety.state()
        if not points:
            return JournalSummary(halted=halt.halted, halt_reason=halt.reason)
        start = points[0].total_nav
        latest = points[-1]
        peak = max(p.peak_nav for p in points) or latest.peak_nav
        max_dd = max((p.drawdown_pct for p in points), default=0.0)
        ret = ((latest.total_nav / start) - 1.0) if start else None
        return JournalSummary(
            points=len(points),
            start_nav=start,
            latest_nav=latest.total_nav,
            peak_nav=peak,
            max_drawdown_pct=max_dd,
            total_return_pct=ret,
            latest=latest,
            halted=halt.halted,
            halt_reason=halt.reason,
        )

    def export_csv(self, path: str | Path, limit: int = 5000) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = list(reversed(self.db.list_equity_snapshots(limit=limit)))
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "timestamp",
                    "equity",
                    "cash",
                    "wallet_usd",
                    "total_nav",
                    "day_pnl",
                    "day_pnl_pct",
                    "peak_nav",
                    "drawdown_pct",
                    "halted",
                    "cycle_id",
                ],
            )
            writer.writeheader()
            for r in rows:
                writer.writerow(
                    {
                        "timestamp": r.timestamp.isoformat(),
                        "equity": r.equity,
                        "cash": r.cash,
                        "wallet_usd": r.wallet_usd,
                        "total_nav": r.total_nav,
                        "day_pnl": r.day_pnl,
                        "day_pnl_pct": r.day_pnl_pct,
                        "peak_nav": r.peak_nav,
                        "drawdown_pct": r.drawdown_pct,
                        "halted": r.halted,
                        "cycle_id": r.cycle_id or "",
                    }
                )
        return path
