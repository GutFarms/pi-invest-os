from __future__ import annotations

from pi_invest.config import RiskConfig
from pi_invest.models import AccountSnapshot, OrderRequest, Side, TradeIntent


class RiskGate:
    """Hard risk limits — every order must pass before the broker sees it."""

    def __init__(self, risk: RiskConfig, universe: list[str]) -> None:
        self.risk = risk
        self.universe = {s.upper() for s in universe}

    def daily_halt(self, account: AccountSnapshot) -> str | None:
        if account.day_pnl_pct <= -abs(self.risk.max_daily_loss_pct):
            return (
                f"daily loss halt: {account.day_pnl_pct:.2%} "
                f"<= -{self.risk.max_daily_loss_pct:.2%}"
            )
        return None

    def filter_intents(
        self,
        intents: list[TradeIntent],
        account: AccountSnapshot,
    ) -> tuple[list[TradeIntent], list[str]]:
        reasons: list[str] = []
        halt = self.daily_halt(account)
        if halt:
            return [], [halt]

        kept: list[TradeIntent] = []
        open_syms = {p.symbol for p in account.positions if p.qty > 0}

        for intent in intents:
            sym = intent.symbol.upper()
            if sym not in self.universe:
                reasons.append(f"{sym}: not in allowlist")
                continue
            if intent.side == Side.HOLD:
                reasons.append(f"{sym}: hold")
                continue
            if intent.confidence < self.risk.min_confidence:
                reasons.append(
                    f"{sym}: confidence {intent.confidence:.2f} "
                    f"< {self.risk.min_confidence:.2f}"
                )
                continue
            if (
                intent.side == Side.BUY
                and sym not in open_syms
                and len(open_syms) + sum(
                    1 for k in kept if k.side == Side.BUY and k.symbol not in open_syms
                )
                >= self.risk.max_open_positions
            ):
                reasons.append(f"{sym}: max open positions reached")
                continue
            if intent.target_weight > self.risk.max_position_pct + 1e-9:
                intent = intent.model_copy(
                    update={"target_weight": self.risk.max_position_pct}
                )
                reasons.append(
                    f"{sym}: capped weight to {self.risk.max_position_pct:.0%}"
                )
            kept.append(intent)
        return kept, reasons

    def to_order(
        self,
        intent: TradeIntent,
        account: AccountSnapshot,
        mark_price: float,
    ) -> tuple[OrderRequest | None, str | None]:
        if mark_price <= 0:
            return None, "bad price"

        deployable = account.equity * (1.0 - self.risk.cash_reserve_pct)
        current_pos = next(
            (p for p in account.positions if p.symbol == intent.symbol), None
        )
        current_value = current_pos.market_value if current_pos else 0.0
        current_weight = current_value / account.equity if account.equity else 0.0

        if intent.side == Side.BUY:
            target_value = deployable * intent.target_weight
            # Cap absolute position to max_position_pct of full equity
            max_value = account.equity * self.risk.max_position_pct
            target_value = min(target_value, max_value)
            delta = target_value - current_value
            if delta < self.risk.min_trade_notional:
                return None, f"buy delta ${delta:.2f} below min notional"
            # Never spend reserved cash
            max_spend = max(0.0, account.cash - account.equity * self.risk.cash_reserve_pct)
            notional = min(delta, max_spend)
            if notional < self.risk.min_trade_notional:
                return None, "insufficient free cash after reserve"
            return (
                OrderRequest(
                    symbol=intent.symbol,
                    side=Side.BUY,
                    notional=notional,
                    reason=intent.rationale,
                    confidence=intent.confidence,
                ),
                None,
            )

        if intent.side == Side.SELL:
            if current_pos is None or current_pos.qty <= 0:
                return None, "nothing to sell"
            # Sell down toward target_weight (0 = full exit)
            target_value = account.equity * max(0.0, intent.target_weight)
            delta = current_value - target_value
            if delta < self.risk.min_trade_notional:
                return None, f"sell delta ${delta:.2f} below min notional"
            qty = min(current_pos.qty, delta / mark_price)
            return (
                OrderRequest(
                    symbol=intent.symbol,
                    side=Side.SELL,
                    qty=qty,
                    reason=intent.rationale,
                    confidence=intent.confidence,
                ),
                None,
            )

        return None, "hold"

    def trim_overweight(
        self, account: AccountSnapshot
    ) -> list[TradeIntent]:
        """Force trim any position above max_position_pct."""
        intents: list[TradeIntent] = []
        if account.equity <= 0:
            return intents
        for pos in account.positions:
            weight = pos.market_value / account.equity
            if weight > self.risk.max_position_pct * 1.05:
                intents.append(
                    TradeIntent(
                        symbol=pos.symbol,
                        side=Side.SELL,
                        target_weight=self.risk.max_position_pct,
                        confidence=1.0,
                        rationale="risk trim: overweight",
                        source="risk",
                    )
                )
        return intents
