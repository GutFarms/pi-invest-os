from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from pi_invest.agent import InvestAgent
from pi_invest.alerts import AlertBus
from pi_invest.broker import PaperBroker
from pi_invest.config import AlertsConfig, AppConfig, EnvSettings
from pi_invest.data import build_market_data
from pi_invest.journal import PerformanceJournal
from pi_invest.models import AccountSnapshot
from pi_invest.safety import SafetyGate
from pi_invest.storage.db import Database
from pi_invest.wallet import build_wallet
from pi_invest.web.app import create_app


def test_alert_drawdown_peak_kv(tmp_path):
    db = Database(tmp_path / "a.db")
    assert db.get_alert_drawdown_peak() == 0.0
    db.set_alert_drawdown_peak(12_345.5)
    assert abs(db.get_alert_drawdown_peak() - 12_345.5) < 1e-9


def test_ntfy_notify_mocked(tmp_path):
    db = Database(tmp_path / "a.db")
    cfg = AlertsConfig(enabled=True, drawdown_alert_pct=0.05)
    env = EnvSettings(ntfy_topic="pi-invest-test", ntfy_server="https://ntfy.sh")
    bus = AlertBus(cfg, env, db)

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    with patch("pi_invest.alerts.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = mock_resp
        assert bus.notify("Hello", "world", tags="test") is True
        client.post.assert_called_once()
        args, kwargs = client.post.call_args
        assert args[0] == "https://ntfy.sh/pi-invest-test"
        assert kwargs["headers"]["Title"] == "Hello"

    # disabled without topic
    bus2 = AlertBus(cfg, EnvSettings(ntfy_topic=""), db)
    assert bus2.notify("x", "y") is False


def test_drawdown_alert_dedupe(tmp_path):
    db = Database(tmp_path / "a.db")
    cfg = AlertsConfig(enabled=True, drawdown_alert_pct=0.05)
    env = EnvSettings(ntfy_topic="dd-test")
    bus = AlertBus(cfg, env, db)
    with patch.object(bus, "notify", return_value=True) as notify:
        bus.drawdown(0.06, nav=9400, peak=10_000)
        bus.drawdown(0.07, nav=9300, peak=10_000)  # same peak → skip
        assert notify.call_count == 1
        bus.drawdown(0.06, nav=10_600 * 0.94, peak=10_600)  # new peak
        assert notify.call_count == 2


def test_halt_fires_alert(tmp_path):
    db = Database(tmp_path / "a.db")
    cfg = AlertsConfig(enabled=True)
    env = EnvSettings(ntfy_topic="halt-test")
    bus = AlertBus(cfg, env, db)
    safety = SafetyGate(db, alerts=bus)
    with patch.object(bus, "notify", return_value=True) as notify:
        safety.halt("stepping away")
        safety.resume()
        assert notify.call_count == 2


def test_journal_triggers_drawdown_alert(tmp_path):
    db = Database(tmp_path / "a.db")
    cfg = AlertsConfig(enabled=True, drawdown_alert_pct=0.05)
    env = EnvSettings(ntfy_topic="j-test")
    bus = AlertBus(cfg, env, db)
    safety = SafetyGate(db, alerts=bus)
    journal = PerformanceJournal(db, safety, alerts=bus)
    with patch.object(bus, "notify", return_value=True) as notify:
        journal.record(AccountSnapshot(cash=10_000, equity=10_000, buying_power=10_000))
        journal.record(
            AccountSnapshot(cash=9_000, equity=9_000, buying_power=9_000, day_pnl=-1000)
        )
        assert notify.call_count == 1


def test_preview_planned_orders(tmp_path):
    db = Database(tmp_path / "p.db")
    safety = SafetyGate(db)
    journal = PerformanceJournal(db, safety)
    cfg = AppConfig()
    cfg.market.provider = "simulator"
    cfg.universe = ["SCHD", "JEPI", "VYM", "BND"]
    cfg.llm.provider = "none"
    broker = PaperBroker(db, 10_000)
    wallet = build_wallet(cfg, EnvSettings(), db, safety=safety)
    market = build_market_data("simulator", allow_simulator_fallback=True)
    agent = InvestAgent(
        cfg, EnvSettings(), market, broker, db, safety=safety, journal=journal, wallet=wallet
    )
    before = broker.get_account({}).cash
    decision = agent.run_cycle(dry_run=True)
    after = broker.get_account({}).cash
    assert abs(before - after) < 1e-9
    assert decision.meta.get("preview") is True
    assert "planned_orders" in decision.meta
    assert decision.orders == []
    assert any("preview" in r or "dry-run" in r for r in decision.skipped_reasons)


def test_readonly_dashboard_forbidden_writes(tmp_path):
    db = Database(tmp_path / "w.db")
    safety = SafetyGate(db)
    journal = PerformanceJournal(db, safety)
    cfg = AppConfig()
    cfg.market.provider = "simulator"
    cfg.dashboard.require_auth = True
    cfg.llm.provider = "none"
    cfg.universe = ["SCHD"]
    env = EnvSettings(
        dashboard_username="pi",
        dashboard_password="secret",
        dashboard_readonly_username="viewer",
        dashboard_readonly_password="look",
        pi_invest_desktop_trust_loopback=False,
    )
    broker = PaperBroker(db, 10_000)
    wallet = build_wallet(cfg, env, db, safety=safety)
    market = build_market_data("simulator", allow_simulator_fallback=True)
    agent = InvestAgent(
        cfg, env, market, broker, db, safety=safety, journal=journal, wallet=wallet
    )
    app = create_app(agent, cfg, db, wallet, env=env, safety=safety, journal=journal)
    client = TestClient(app)

    # viewer can read status
    r = client.get("/api/status", auth=("viewer", "look"))
    assert r.status_code == 200
    assert r.json()["role"] == "viewer"

    # viewer cannot halt
    r = client.post("/api/halt", json={"reason": "nope"}, auth=("viewer", "look"))
    assert r.status_code == 403

    # viewer cannot send
    r = client.post(
        "/api/wallet/send",
        json={
            "asset": "USD",
            "amount": 1,
            "to_address": "usd:x",
            "confirm": "SEND 1.00 USD",
        },
        auth=("viewer", "look"),
    )
    assert r.status_code == 403

    # admin can halt
    r = client.post("/api/halt", json={"reason": "admin"}, auth=("pi", "secret"))
    assert r.status_code == 200
    assert r.json()["halted"] is True

    # bad creds
    r = client.get("/api/status", auth=("viewer", "wrong"))
    assert r.status_code == 401
