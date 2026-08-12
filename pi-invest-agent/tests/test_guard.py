from __future__ import annotations

from pi_invest.agent.guard import InvestmentGuard
from pi_invest.config import GuardConfig
from pi_invest.models import Side, SignalScore, TradeIntent


def _score(sym: str, proxy: float, composite: float | None = None) -> SignalScore:
    c = composite if composite is not None else proxy
    return SignalScore(
        symbol=sym,
        momentum=0.5,
        yield_score=proxy,
        trend=0.5,
        volatility_penalty=0.2,
        composite=c,
        expected_income_proxy=proxy,
        notes=[],
    )


def test_guard_blocks_low_income_llm_buy():
    guard = InvestmentGuard(GuardConfig(enabled=True, require_score_agreement=False))
    scores = [_score("NVDA", 0.30), _score("SCHD", 0.70)]
    llm = [
        TradeIntent(
            symbol="NVDA",
            side=Side.BUY,
            target_weight=0.12,
            confidence=0.9,
            rationale="moon",
            source="llm",
        )
    ]
    heur = [
        TradeIntent(
            symbol="SCHD",
            side=Side.BUY,
            target_weight=0.12,
            confidence=0.7,
            rationale="income",
            source="heuristic",
        )
    ]
    kept, notes, meta = guard.merge_intents(scores, llm, heur)
    assert all(not (i.symbol == "NVDA" and i.side == Side.BUY) for i in kept)
    assert meta["blocked"]
    assert any(i.symbol == "SCHD" for i in kept)


def test_guard_requires_agreement_unless_strong():
    guard = InvestmentGuard(
        GuardConfig(
            enabled=True,
            require_score_agreement=True,
            min_buy_income_proxy=0.40,
            agreement_bypass_proxy=0.62,
        )
    )
    scores = [_score("QQQ", 0.50), _score("SCHD", 0.70)]
    llm = [
        TradeIntent(
            symbol="QQQ",
            side=Side.BUY,
            target_weight=0.1,
            confidence=0.6,
            rationale="tech",
            source="llm",
        )
    ]
    heur = [
        TradeIntent(
            symbol="SCHD",
            side=Side.BUY,
            target_weight=0.1,
            confidence=0.7,
            rationale="income",
            source="heuristic",
        )
    ]
    kept, _, meta = guard.merge_intents(scores, llm, heur)
    assert any("QQQ" in b for b in meta["blocked"])
    assert any(i.symbol == "SCHD" for i in kept)


def test_guard_blocks_sell_of_healthy_income():
    guard = InvestmentGuard(GuardConfig(enabled=True, require_score_agreement=True))
    scores = [_score("JEPI", 0.65)]
    llm = [
        TradeIntent(
            symbol="JEPI",
            side=Side.SELL,
            target_weight=0.0,
            confidence=0.8,
            rationale="rotate",
            source="llm",
        )
    ]
    kept, _, meta = guard.merge_intents(scores, llm, [])
    assert kept == []
    assert any("JEPI" in b for b in meta["blocked"])


def test_drawdown_halts_buys():
    guard = InvestmentGuard(
        GuardConfig(
            enabled=True,
            drawdown_throttle_pct=0.04,
            drawdown_halt_buys_pct=0.08,
        )
    )
    intents = [
        TradeIntent(
            symbol="SCHD",
            side=Side.BUY,
            target_weight=0.12,
            confidence=0.7,
            rationale="x",
            source="heuristic",
        ),
        TradeIntent(
            symbol="NVDA",
            side=Side.SELL,
            target_weight=0.0,
            confidence=0.7,
            rationale="x",
            source="heuristic",
        ),
    ]
    out, notes = guard.apply_drawdown_throttle(intents, max_drawdown_pct=0.09)
    assert all(i.side != Side.BUY for i in out)
    assert any(i.side == Side.SELL for i in out)
    assert notes


def test_drawdown_soft_throttle_halves_size():
    guard = InvestmentGuard(
        GuardConfig(drawdown_throttle_pct=0.04, drawdown_halt_buys_pct=0.08)
    )
    intents = [
        TradeIntent(
            symbol="SCHD",
            side=Side.BUY,
            target_weight=0.12,
            confidence=0.7,
            rationale="x",
            source="heuristic",
        )
    ]
    out, notes = guard.apply_drawdown_throttle(intents, max_drawdown_pct=0.05)
    assert len(out) == 1
    assert out[0].target_weight == 0.06
    assert notes
