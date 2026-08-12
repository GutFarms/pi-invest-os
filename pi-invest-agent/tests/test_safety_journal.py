from __future__ import annotations

import pytest

from pi_invest.broker import PaperBroker
from pi_invest.config import AppConfig, EnvSettings, WalletConfig
from pi_invest.journal import PerformanceJournal
from pi_invest.models import AccountSnapshot
from pi_invest.safety import HaltedError, SafetyGate
from pi_invest.storage.db import Database
from pi_invest.wallet import PaperWallet, WalletError, WalletService, build_wallet


def test_halt_blocks_send(tmp_path):
    db = Database(tmp_path / "s.db")
    safety = SafetyGate(db)
    cfg = WalletConfig(
        starting_balances={"USD": 500.0, "BTC": 0, "ETH": 0, "USDC": 0},
        allowlist_required=False,
        require_send_confirmation=False,
    )
    svc = WalletService(PaperWallet(db, cfg), db, cfg, EnvSettings(), safety=safety)
    safety.halt("test")
    with pytest.raises(WalletError, match="halted"):
        svc.send("USD", 10, "usd:x")
    # inbound still ok
    svc.receive("USD", 5, from_address="friend")
    safety.resume()
    svc.send("USD", 10, "usd:x")


def test_daily_send_limit(tmp_path):
    db = Database(tmp_path / "s.db")
    safety = SafetyGate(db)
    cfg = WalletConfig(
        starting_balances={"USD": 1000.0, "BTC": 0, "ETH": 0, "USDC": 0},
        max_daily_send_usd=50.0,
        allowlist_required=False,
        require_send_confirmation=False,
    )
    svc = WalletService(PaperWallet(db, cfg), db, cfg, EnvSettings(), safety=safety)
    svc.send("USD", 40, "usd:a")
    with pytest.raises(WalletError, match="daily send limit"):
        svc.send("USD", 20, "usd:b")


def test_journal_drawdown(tmp_path):
    db = Database(tmp_path / "s.db")
    safety = SafetyGate(db)
    journal = PerformanceJournal(db, safety)
    a1 = AccountSnapshot(cash=10_000, equity=10_000, buying_power=10_000)
    s1 = journal.record(a1, None)
    assert s1.total_nav == 10_000
    assert s1.drawdown_pct == 0.0
    a2 = AccountSnapshot(cash=9_000, equity=9_000, buying_power=9_000, day_pnl=-1000)
    s2 = journal.record(a2, None)
    assert s2.peak_nav == 10_000
    assert abs(s2.drawdown_pct - 0.1) < 1e-9
    summary = journal.summary()
    assert summary.points == 2
    assert abs(summary.max_drawdown_pct - 0.1) < 1e-9
    path = journal.export_csv(tmp_path / "j.csv")
    assert path.exists()
    text = path.read_text()
    assert "total_nav" in text


def test_halt_blocks_trading_cycle(tmp_path):
    from pi_invest.agent import InvestAgent
    from pi_invest.data import build_market_data

    db = Database(tmp_path / "s.db")
    safety = SafetyGate(db)
    journal = PerformanceJournal(db, safety)
    cfg = AppConfig()
    cfg.market.provider = "simulator"
    cfg.universe = ["SCHD", "JEPI"]
    cfg.llm.provider = "none"
    broker = PaperBroker(db, 10_000)
    wallet = build_wallet(cfg, EnvSettings(), db, safety=safety)
    agent = InvestAgent(
        cfg,
        EnvSettings(),
        build_market_data("simulator"),
        broker,
        db,
        safety=safety,
        journal=journal,
        wallet=wallet,
    )
    safety.halt("unit test")
    decision = agent.run_cycle(dry_run=False)
    assert decision.meta.get("halted") is True
    assert decision.orders == []
    assert any("HALTED" in r or "halt" in r.lower() for r in decision.skipped_reasons)
    # journal still recorded
    assert journal.summary().points >= 1


def test_safety_assert(tmp_path):
    db = Database(tmp_path / "s.db")
    gate = SafetyGate(db)
    gate.assert_trading_allowed()
    gate.halt("x")
    with pytest.raises(HaltedError):
        gate.assert_trading_allowed()
