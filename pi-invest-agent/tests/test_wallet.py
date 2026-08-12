from __future__ import annotations

import pytest

from pi_invest.broker import PaperBroker
from pi_invest.config import AppConfig, EnvSettings, WalletConfig
from pi_invest.storage.db import Database
from pi_invest.wallet import PaperWallet, WalletError, WalletService, build_wallet
from pi_invest.wallet.confirm import confirmation_phrase


def open_cfg(**kwargs) -> WalletConfig:
    """Test helper: disable interactive send gates unless requested."""
    base = dict(
        allowlist_required=False,
        require_send_confirmation=False,
    )
    base.update(kwargs)
    return WalletConfig(**base)


def test_receive_address_stable(tmp_path):
    db = Database(tmp_path / "w.db")
    cfg = open_cfg()
    w1 = PaperWallet(db, cfg)
    w2 = PaperWallet(db, cfg)
    a1 = w1.receive_address("BTC")
    a2 = w2.receive_address("BTC")
    assert a1.address == a2.address
    assert a1.address.startswith("paper-btc-")
    usd = w1.receive_address("USD")
    assert usd.address.startswith("usd:piinvest:")


def test_credit_and_send_btc(tmp_path):
    db = Database(tmp_path / "w.db")
    cfg = open_cfg(max_daily_send_usd=10_000.0)
    svc = WalletService(PaperWallet(db, cfg), db, cfg, EnvSettings())
    svc.receive("BTC", 0.05, from_address="friend")
    snap = svc.snapshot()
    btc = next(b for b in snap.balances if b.asset == "BTC")
    assert abs(btc.amount - 0.05) < 1e-9

    sent = svc.send("BTC", 0.01, "paper-btc-elsewhere", memo="thanks")
    assert sent.direction.value == "send"
    assert sent.fee > 0
    snap2 = svc.snapshot()
    btc2 = next(b for b in snap2.balances if b.asset == "BTC")
    assert abs(btc2.amount - (0.05 - 0.01 - sent.fee)) < 1e-9


def test_send_usd_and_insufficient(tmp_path):
    db = Database(tmp_path / "w.db")
    cfg = open_cfg(
        starting_balances={"USD": 50.0, "BTC": 0.0, "ETH": 0.0, "USDC": 0.0}
    )
    svc = WalletService(PaperWallet(db, cfg), db, cfg, EnvSettings())
    svc.send("USD", 25.0, "usd:friend:xyz")
    with pytest.raises(WalletError):
        svc.send("USD", 1000.0, "usd:friend:xyz")


def test_bridge_wallet_broker(tmp_path):
    db = Database(tmp_path / "w.db")
    PaperBroker(db, starting_cash=5_000)
    cfg = open_cfg(
        starting_balances={"USD": 200.0, "BTC": 0.0, "ETH": 0.0, "USDC": 0.0}
    )
    svc = WalletService(PaperWallet(db, cfg), db, cfg, EnvSettings())

    svc.bridge_to_broker(100.0)
    cash, _, _ = db.load_paper_state()
    assert abs(cash - 5_100) < 1e-6
    assert abs(db.wallet_balances()["USD"] - 100.0) < 1e-6

    svc.bridge_from_broker(50.0)
    cash2, _, _ = db.load_paper_state()
    assert abs(cash2 - 5_050) < 1e-6
    assert abs(db.wallet_balances()["USD"] - 150.0) < 1e-6


def test_build_wallet_from_appconfig(tmp_path, monkeypatch):
    db = Database(tmp_path / "w.db")
    cfg = AppConfig()
    env = EnvSettings(pi_invest_db=str(tmp_path / "w.db"))
    wallet = build_wallet(cfg, env, db)
    snap = wallet.snapshot()
    assert snap.backend == "paper"
    assert any(b.asset == "USD" for b in snap.balances)


def test_history_records(tmp_path):
    db = Database(tmp_path / "w.db")
    cfg = open_cfg(max_daily_send_usd=10_000.0)
    svc = WalletService(PaperWallet(db, cfg), db, cfg, EnvSettings())
    svc.receive("ETH", 1.0)
    svc.send("ETH", 0.1, "0xabc")
    hist = svc.history(10)
    assert len(hist) >= 2
    assert {h.direction.value for h in hist} >= {"send", "receive"}


def test_allowlist_and_confirmation(tmp_path):
    db = Database(tmp_path / "w.db")
    cfg = WalletConfig(
        starting_balances={"USD": 500.0, "BTC": 0, "ETH": 0, "USDC": 0},
        allowlist_required=True,
        require_send_confirmation=True,
        max_daily_send_usd=10_000,
    )
    svc = WalletService(PaperWallet(db, cfg), db, cfg, EnvSettings())
    with pytest.raises(WalletError, match="confirmation required"):
        svc.send("USD", 10, "usd:friend")
    phrase = confirmation_phrase("USD", 10)
    with pytest.raises(WalletError, match="allowlist"):
        svc.send("USD", 10, "usd:friend", confirm=phrase)
    svc.allowlist_add("usd:friend", label="buddy")
    record = svc.send("USD", 10, "usd:friend", confirm=phrase)
    assert record.amount == 10
    assert record.counterparty == "usd:friend"
    audit = db.recent_audit(5)
    assert any(a["kind"] == "wallet.send" for a in audit)
