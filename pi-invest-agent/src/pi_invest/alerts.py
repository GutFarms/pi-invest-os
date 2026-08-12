from __future__ import annotations

from typing import Any

import httpx

from pi_invest.config import AlertsConfig, EnvSettings
from pi_invest.storage.db import Database


class AlertBus:
    """Fire-and-forget notifications (ntfy) with light dedupe via SQLite."""

    def __init__(
        self,
        cfg: AlertsConfig,
        env: EnvSettings,
        db: Database | None = None,
    ) -> None:
        self.cfg = cfg
        self.env = env
        self.db = db

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.env.ntfy_topic.strip())

    def notify(
        self,
        title: str,
        message: str,
        *,
        tags: str = "warning",
        priority: int = 3,
        kind: str = "alert",
    ) -> bool:
        if not self.enabled:
            return False
        server = self.env.ntfy_server.rstrip("/")
        topic = self.env.ntfy_topic.strip()
        url = f"{server}/{topic}"
        headers = {
            "Title": title[:120],
            "Tags": tags,
            "Priority": str(priority),
        }
        if self.env.ntfy_token:
            headers["Authorization"] = f"Bearer {self.env.ntfy_token}"
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, content=message.encode("utf-8"), headers=headers)
                resp.raise_for_status()
        except Exception:  # noqa: BLE001
            return False
        if self.db is not None:
            try:
                self.db.audit(kind, f"{title}: {message[:200]}")
            except Exception:  # noqa: BLE001
                pass
        return True

    def halt(self, reason: str) -> None:
        self.notify(
            "Pi Invest HALTED",
            reason or "kill switch active",
            tags="no_entry,warning",
            priority=4,
            kind="alert.halt",
        )

    def resume(self) -> None:
        self.notify(
            "Pi Invest resumed",
            "Trading and sends unlocked",
            tags="white_check_mark",
            priority=2,
            kind="alert.resume",
        )

    def drawdown(self, drawdown_pct: float, nav: float, peak: float) -> None:
        if drawdown_pct + 1e-12 < self.cfg.drawdown_alert_pct:
            return
        if self.db is not None:
            last = self.db.get_alert_drawdown_peak()
            # Only re-alert when we make a new peak then draw down again,
            # or first time past threshold for this peak.
            if last > 0 and abs(last - peak) < 1e-6:
                return
            self.db.set_alert_drawdown_peak(peak)
        self.notify(
            "Pi Invest drawdown",
            f"Drawdown {drawdown_pct:.1%} from peak ${peak:,.2f} (NAV ${nav:,.2f})",
            tags="chart_with_downwards_trend,warning",
            priority=4,
            kind="alert.drawdown",
        )

    def coinbase_error(self, detail: str) -> None:
        self.notify(
            "Coinbase connection error",
            detail[:300],
            tags="cloud,x",
            priority=3,
            kind="alert.coinbase",
        )


def build_alerts(
    cfg: AlertsConfig, env: EnvSettings, db: Database | None = None
) -> AlertBus:
    return AlertBus(cfg, env, db)
