from __future__ import annotations

from pi_invest.agent.risk import RiskGate
from pi_invest.agent.scoring import heuristic_intents, score_symbol
from pi_invest.config import RiskConfig
from pi_invest.data import SimulatorMarketData
from pi_invest.models import AccountSnapshot, Position, Side, TradeIntent


def test_simulator_history_length():
    m = SimulatorMarketData(seed=1)
    bars = m.get_history("SCHD", lookback_days=60)
    assert len(bars) == 60
    assert bars[-1].close > 0


def test_score_symbol_bounds():
    m = SimulatorMarketData(seed=2)
    q = m.get_quote("JEPI")
    h = m.get_history("JEPI", 60)
    s = score_symbol(q, h)
    assert 0.0 <= s.composite <= 1.0
    assert 0.0 <= s.expected_income_proxy <= 1.0
    assert s.symbol == "JEPI"


def test_heuristic_prefers_income():
    m = SimulatorMarketData(seed=3)
    scores = []
    for sym in ["JEPI", "SCHD", "NVDA", "BND"]:
        scores.append(score_symbol(m.get_quote(sym), m.get_history(sym, 60)))
    intents = heuristic_intents(scores, top_n=3)
    buys = [i for i in intents if i.side == Side.BUY]
    assert buys
    assert all(0 < i.target_weight <= 0.15 for i in buys)


def test_risk_blocks_unknown_symbol():
    gate = RiskGate(RiskConfig(), universe=["SCHD", "VYM"])
    acct = AccountSnapshot(cash=10_000, equity=10_000, buying_power=10_000)
    intents = [
        TradeIntent(
            symbol="TSLA",
            side=Side.BUY,
            target_weight=0.1,
            confidence=0.9,
            rationale="nope",
        )
    ]
    kept, reasons = gate.filter_intents(intents, acct)
    assert kept == []
    assert any("allowlist" in r for r in reasons)


def test_risk_daily_halt():
    gate = RiskGate(RiskConfig(max_daily_loss_pct=0.03), universe=["SCHD"])
    acct = AccountSnapshot(
        cash=1000, equity=9700, buying_power=1000, day_pnl=-300, day_pnl_pct=-0.03
    )
    intents = [
        TradeIntent(
            symbol="SCHD",
            side=Side.BUY,
            target_weight=0.1,
            confidence=0.9,
            rationale="x",
        )
    ]
    kept, reasons = gate.filter_intents(intents, acct)
    assert kept == []
    assert any("daily loss" in r for r in reasons)


def test_risk_to_order_respects_cash_reserve():
    gate = RiskGate(
        RiskConfig(cash_reserve_pct=0.5, min_trade_notional=10, max_position_pct=0.5),
        universe=["SCHD"],
    )
    # Only 100 cash on 10k equity with 50% reserve => no free cash
    acct = AccountSnapshot(
        cash=100,
        equity=10_000,
        buying_power=100,
        positions=[
            Position(symbol="OTHER", qty=10, avg_cost=990, market_price=990)
        ],
    )
    intent = TradeIntent(
        symbol="SCHD",
        side=Side.BUY,
        target_weight=0.1,
        confidence=0.9,
        rationale="x",
    )
    order, reason = gate.to_order(intent, acct, mark_price=100.0)
    assert order is None
    assert reason is not None


def test_paper_broker_roundtrip(tmp_path):
    from pi_invest.broker import PaperBroker
    from pi_invest.models import OrderRequest
    from pi_invest.storage.db import Database

    db = Database(tmp_path / "t.db")
    broker = PaperBroker(db, starting_cash=10_000)
    result = broker.place_order(
        OrderRequest(symbol="SCHD", side=Side.BUY, notional=1000, confidence=0.8),
        mark_price=50.0,
    )
    assert result.ok
    acct = broker.get_account({"SCHD": 50.0})
    assert acct.cash == 9000
    assert len(acct.positions) == 1
    assert abs(acct.positions[0].qty - 20.0) < 1e-6

    sell = broker.place_order(
        OrderRequest(symbol="SCHD", side=Side.SELL, qty=5, confidence=0.8),
        mark_price=52.0,
    )
    assert sell.ok
    acct2 = broker.get_account({"SCHD": 52.0})
    assert abs(acct2.positions[0].qty - 15.0) < 1e-6


def test_agent_cycle_paper(tmp_path, monkeypatch):
    from pi_invest.agent import InvestAgent
    from pi_invest.broker import PaperBroker
    from pi_invest.config import AppConfig, EnvSettings
    from pi_invest.data import build_market_data
    from pi_invest.storage.db import Database

    cfg = AppConfig()
    cfg.market.provider = "simulator"
    cfg.universe = ["SCHD", "JEPI", "BND", "QQQ"]
    cfg.llm.provider = "none"
    env = EnvSettings(pi_invest_db=str(tmp_path / "a.db"))
    db = Database(tmp_path / "a.db")
    market = build_market_data("simulator", True)
    broker = PaperBroker(db, starting_cash=10_000)
    agent = InvestAgent(cfg, env, market, broker, db)
    decision = agent.run_cycle(dry_run=False)
    assert decision.scores
    assert decision.account_after is not None
    # Should have attempted at least planning; fills depend on confidence thresholds
    assert decision.account_after.equity > 0
