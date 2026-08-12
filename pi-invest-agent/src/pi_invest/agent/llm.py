from __future__ import annotations

import json
import re
from typing import Any

import httpx

from pi_invest.config import EnvSettings, LlmConfig
from pi_invest.models import AccountSnapshot, Side, SignalScore, TradeIntent


SYSTEM_PROMPT = """You are an income-focused portfolio assistant running on a Raspberry Pi.
Your job is expected income (dividends + durable appreciation), not speculation.

Given scored tickers and the current account, propose a SMALL set of trades.
A separate investment guard will REJECT any idea that disagrees with the
quantitative income scores — so align with high expected_income_proxy names.

Return ONLY valid JSON with this shape:
{
  "intents": [
    {
      "symbol": "SCHD",
      "side": "buy"|"sell"|"hold",
      "target_weight": 0.12,
      "confidence": 0.0-1.0,
      "rationale": "short reason"
    }
  ],
  "summary": "one sentence"
}

Rules:
- Only use symbols from the provided universe/scores.
- Prefer diversified income ETFs (SCHD, VYM, JEPI, BND) over speculative single names.
- Prefer buys where expected_income_proxy is strong; do not sell strong income names.
- target_weight must be between 0 and 0.15.
- confidence must not greatly exceed the symbol's composite score.
- At most 4 buy intents. Be concise. No markdown.
"""


class LlmAdvisor:
    def __init__(self, cfg: LlmConfig, env: EnvSettings) -> None:
        self.cfg = cfg
        self.env = env

    def enabled(self) -> bool:
        return self.cfg.provider != "none"

    def advise(
        self,
        scores: list[SignalScore],
        account: AccountSnapshot,
        universe: list[str],
    ) -> tuple[list[TradeIntent], str | None]:
        if not self.enabled():
            return [], None

        payload = {
            "universe": universe,
            "scores": [s.model_dump() for s in scores],
            "cash": account.cash,
            "equity": account.equity,
            "positions": [p.model_dump() for p in account.positions],
        }
        user = (
            "Propose trades to maximize expected income.\n"
            + json.dumps(payload, default=str)
        )

        try:
            if self.cfg.provider == "ollama":
                raw = self._ollama(user)
            elif self.cfg.provider == "openai":
                if self.env.pi_invest_local_only:
                    return [], "llm error: OpenAI blocked (PI_INVEST_LOCAL_ONLY)"
                raw = self._openai(user)
            else:
                return [], None
        except Exception as exc:  # noqa: BLE001
            return [], f"llm error: {exc}"

        intents = _parse_intents(raw, universe)
        return intents, raw

    def _ollama(self, user: str) -> str:
        url = f"{self.env.ollama_base_url.rstrip('/')}/api/chat"
        body = {
            "model": self.env.ollama_model,
            "stream": False,
            "options": {"temperature": self.cfg.temperature},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
        }
        with httpx.Client(timeout=self.cfg.timeout_seconds) as client:
            r = client.post(url, json=body)
            r.raise_for_status()
            data = r.json()
        return str((data.get("message") or {}).get("content") or "")

    def _openai(self, user: str) -> str:
        if not self.env.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY missing")
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.env.openai_api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.env.openai_model,
            "temperature": self.cfg.temperature,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
        }
        with httpx.Client(timeout=self.cfg.timeout_seconds) as client:
            r = client.post(url, headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
        return str(data["choices"][0]["message"]["content"])


def _extract_json(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise
        return json.loads(match.group(0))


def _parse_intents(raw: str, universe: list[str]) -> list[TradeIntent]:
    allowed = {s.upper() for s in universe}
    data = _extract_json(raw)
    items = data.get("intents") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out: list[TradeIntent] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol", "")).upper()
        if sym not in allowed:
            continue
        side_raw = str(item.get("side", "hold")).lower()
        try:
            side = Side(side_raw)
        except ValueError:
            continue
        out.append(
            TradeIntent(
                symbol=sym,
                side=side,
                target_weight=min(0.15, max(0.0, float(item.get("target_weight") or 0.0))),
                confidence=min(1.0, max(0.0, float(item.get("confidence") or 0.0))),
                rationale=str(item.get("rationale") or "llm"),
                source="llm",
            )
        )
    return out
