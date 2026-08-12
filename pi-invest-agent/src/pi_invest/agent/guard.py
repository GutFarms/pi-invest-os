"""Alinia-inspired investment guardrails for on-device Pi trading.

Validates LLM trade proposals against quantitative income scores before risk
gates see them. Improves expected edge by concentrating in high-quality income
names and blocking hallucinated / reckless advice — without unlocking live risk.
"""

from __future__ import annotations

from pi_invest.config import GuardConfig
from pi_invest.models import Side, SignalScore, TradeIntent


# Core income / ballast names — prefer when scores are close
INCOME_PREFERRED = frozenset(
    {"SCHD", "VYM", "JEPI", "JEPQ", "DIVO", "BND", "VGIT", "TLT", "QYLD", "XYLD"}
)


class InvestmentGuard:
    """Regulation-style policy layer between LLM output and RiskGate."""

    def __init__(self, cfg: GuardConfig) -> None:
        self.cfg = cfg

    def merge_intents(
        self,
        scores: list[SignalScore],
        llm_intents: list[TradeIntent],
        heuristic: list[TradeIntent],
    ) -> tuple[list[TradeIntent], list[str], dict]:
        """Combine LLM + heuristic plans under guard policy.

        Returns (intents, skip_notes, audit_meta).
        """
        notes: list[str] = []
        meta: dict = {
            "llm_in": len(llm_intents),
            "heuristic_in": len(heuristic),
            "allowed": [],
            "blocked": [],
        }

        if not self.cfg.enabled:
            chosen = list(llm_intents) if llm_intents else list(heuristic)
            meta["mode"] = "unguarded"
            meta["allowed"] = [f"{i.source}:{i.side.value}:{i.symbol}" for i in chosen]
            return chosen, notes, meta

        by_sym = {s.symbol.upper(): s for s in scores}
        heur_buys = {
            i.symbol.upper()
            for i in heuristic
            if i.side == Side.BUY
        }
        heur_sells = {
            i.symbol.upper()
            for i in heuristic
            if i.side == Side.SELL
        }

        kept: list[TradeIntent] = []

        # Always keep risk trims that arrive as source=risk (caller may prepend)
        for intent in llm_intents:
            ok, reason, fixed = self._validate_llm(intent, by_sym, heur_buys, heur_sells)
            label = f"llm:{intent.side.value}:{intent.symbol.upper()}"
            if ok and fixed is not None:
                kept.append(fixed)
                meta["allowed"].append(label)
                if reason:
                    notes.append(reason)
            else:
                meta["blocked"].append(f"{label} ({reason})")
                notes.append(f"guard blocked {label}: {reason}")

        # Fill with heuristics for symbols not already covered (buys/sells)
        covered = {(i.side, i.symbol.upper()) for i in kept}
        for intent in heuristic:
            key = (intent.side, intent.symbol.upper())
            if key in covered:
                continue
            # When require_score_agreement and LLM ran, still allow heuristic
            # buys/sells that passed scoring — they are the quantitative floor.
            boosted = self._prefer_income(intent, by_sym)
            kept.append(boosted)
            meta["allowed"].append(
                f"{boosted.source}:{boosted.side.value}:{boosted.symbol}"
            )
            covered.add(key)

        # Prefer income ETFs: if two buys compete and we exceed soft count, drop
        # lowest non-income first (risk gate still caps positions).
        kept = self._rank_buys_for_income(kept, by_sym)
        meta["mode"] = "guarded"
        meta["out"] = len(kept)
        return kept, notes, meta

    def apply_drawdown_throttle(
        self,
        intents: list[TradeIntent],
        max_drawdown_pct: float,
    ) -> tuple[list[TradeIntent], list[str]]:
        """Block or shrink new buys when NAV drawdown is elevated."""
        if not self.cfg.enabled:
            return intents, []
        dd = abs(float(max_drawdown_pct or 0.0))
        notes: list[str] = []
        hard = abs(self.cfg.drawdown_halt_buys_pct)
        soft = abs(self.cfg.drawdown_throttle_pct)

        if dd < soft:
            return intents, notes

        out: list[TradeIntent] = []
        for intent in intents:
            if intent.side != Side.BUY:
                out.append(intent)
                continue
            if intent.source == "risk":
                out.append(intent)
                continue
            if dd >= hard:
                notes.append(
                    f"guard DD halt buy {intent.symbol}: "
                    f"drawdown {dd:.2%} >= {hard:.2%}"
                )
                continue
            # Soft throttle: cut target weight in half
            scaled = intent.model_copy(
                update={
                    "target_weight": round(intent.target_weight * 0.5, 4),
                    "rationale": (
                        f"{intent.rationale} | DD throttle "
                        f"{dd:.2%}→half size"
                    ),
                }
            )
            notes.append(
                f"guard DD throttle {intent.symbol}: "
                f"drawdown {dd:.2%} — halved buy size"
            )
            out.append(scaled)
        return out, notes

    def _validate_llm(
        self,
        intent: TradeIntent,
        by_sym: dict[str, SignalScore],
        heur_buys: set[str],
        heur_sells: set[str],
    ) -> tuple[bool, str, TradeIntent | None]:
        sym = intent.symbol.upper()
        score = by_sym.get(sym)
        if score is None:
            return False, "no score for symbol", None
        if intent.side == Side.HOLD:
            return False, "hold ignored", None

        # Clamp hallucinated weights / confidence
        max_w = self.cfg.max_llm_target_weight
        tw = max(0.0, min(float(intent.target_weight), max_w))
        conf = max(0.0, min(float(intent.confidence), 1.0))
        # Cap confidence relative to composite (anti-hallucination)
        max_conf = min(1.0, score.composite + self.cfg.max_confidence_above_composite)
        if conf > max_conf:
            conf = max_conf

        fixed = intent.model_copy(
            update={
                "symbol": sym,
                "target_weight": round(tw, 4),
                "confidence": round(conf, 4),
            }
        )

        if intent.side == Side.BUY:
            if score.expected_income_proxy < self.cfg.min_buy_income_proxy:
                return (
                    False,
                    f"income proxy {score.expected_income_proxy:.2f} "
                    f"< {self.cfg.min_buy_income_proxy:.2f}",
                    None,
                )
            if (
                self.cfg.require_score_agreement
                and sym not in heur_buys
                and score.expected_income_proxy < self.cfg.agreement_bypass_proxy
            ):
                return (
                    False,
                    "no heuristic agreement (score not in buy set)",
                    None,
                )
            fixed = self._prefer_income(fixed, by_sym)
            return True, "", fixed

        if intent.side == Side.SELL:
            # Do not dump strong income names on LLM whim
            if score.expected_income_proxy >= self.cfg.max_sell_income_proxy:
                if self.cfg.require_score_agreement and sym not in heur_sells:
                    return (
                        False,
                        f"refuse sell of healthy income proxy "
                        f"{score.expected_income_proxy:.2f}",
                        None,
                    )
            return True, "", fixed

        return False, "unsupported side", None

    def _prefer_income(
        self, intent: TradeIntent, by_sym: dict[str, SignalScore]
    ) -> TradeIntent:
        if not self.cfg.prefer_income_etfs or intent.side != Side.BUY:
            return intent
        sym = intent.symbol.upper()
        if sym not in INCOME_PREFERRED:
            return intent
        boost = self.cfg.income_etf_boost
        score = by_sym.get(sym)
        new_w = min(
            self.cfg.max_llm_target_weight,
            intent.target_weight + boost,
        )
        new_c = min(1.0, intent.confidence + boost * 0.5)
        if score is not None:
            new_c = min(1.0, max(new_c, min(1.0, score.composite + boost)))
        note = intent.rationale
        if "income-preferred" not in note:
            note = f"{note} | income-preferred ETF"
        return intent.model_copy(
            update={
                "target_weight": round(new_w, 4),
                "confidence": round(new_c, 4),
                "rationale": note,
            }
        )

    def _rank_buys_for_income(
        self, intents: list[TradeIntent], by_sym: dict[str, SignalScore]
    ) -> list[TradeIntent]:
        """Stable sort: sells/risk first, then buys by income proxy desc."""
        sells = [i for i in intents if i.side != Side.BUY]
        buys = [i for i in intents if i.side == Side.BUY]

        def buy_key(i: TradeIntent) -> tuple:
            s = by_sym.get(i.symbol.upper())
            proxy = s.expected_income_proxy if s else 0.0
            preferred = 1 if i.symbol.upper() in INCOME_PREFERRED else 0
            return (preferred, proxy, i.confidence)

        buys_sorted = sorted(buys, key=buy_key, reverse=True)
        # Soft cap: keep top N buys so capital concentrates (profitability)
        top_n = max(1, self.cfg.max_buy_intents)
        buys_sorted = buys_sorted[:top_n]
        return sells + buys_sorted
