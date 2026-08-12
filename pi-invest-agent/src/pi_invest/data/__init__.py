from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx

from pi_invest.models import Bar, Quote, utcnow


class MarketDataProvider(Protocol):
    def get_quote(self, symbol: str) -> Quote: ...

    def get_history(self, symbol: str, lookback_days: int = 60) -> list[Bar]: ...


# Deterministic-ish simulator seeds per symbol for reproducible demos
_SIM_BASE: dict[str, tuple[float, float]] = {
    "SCHD": (78.0, 0.035),
    "VYM": (118.0, 0.030),
    "JEPI": (56.0, 0.080),
    "QQQ": (480.0, 0.006),
    "SPY": (520.0, 0.013),
    "BND": (72.0, 0.038),
    "GLD": (220.0, 0.0),
    "AAPL": (190.0, 0.005),
    "MSFT": (420.0, 0.007),
    "NVDA": (900.0, 0.0003),
}


class SimulatorMarketData:
    """Offline quotes/history so the Pi agent can run without internet."""

    def __init__(self, seed: int = 42) -> None:
        self._rng = random.Random(seed)

    def _base(self, symbol: str) -> tuple[float, float]:
        return _SIM_BASE.get(symbol.upper(), (100.0, 0.02))

    def get_quote(self, symbol: str) -> Quote:
        price, dy = self._base(symbol)
        noise = self._rng.uniform(-0.012, 0.012)
        px = round(price * (1 + noise), 4)
        return Quote(
            symbol=symbol.upper(),
            price=px,
            previous_close=round(price, 4),
            dividend_yield=dy,
            volume=1_000_000,
        )

    def get_history(self, symbol: str, lookback_days: int = 60) -> list[Bar]:
        price, _ = self._base(symbol)
        bars: list[Bar] = []
        px = price * 0.95
        now = utcnow()
        local_rng = random.Random(hash(symbol.upper()) ^ 0xC0FFEE)
        for i in range(lookback_days, 0, -1):
            ret = local_rng.gauss(0.0004, 0.012)
            open_px = px
            close = max(0.5, px * (1 + ret))
            high = max(open_px, close) * (1 + abs(local_rng.gauss(0, 0.004)))
            low = min(open_px, close) * (1 - abs(local_rng.gauss(0, 0.004)))
            bars.append(
                Bar(
                    symbol=symbol.upper(),
                    ts=now - timedelta(days=i),
                    open=round(open_px, 4),
                    high=round(high, 4),
                    low=round(low, 4),
                    close=round(close, 4),
                    volume=local_rng.uniform(5e5, 5e6),
                )
            )
            px = close
        return bars


class YahooMarketData:
    """Lightweight Yahoo Finance chart API client (no official SDK)."""

    CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    def __init__(self, timeout: float = 20.0) -> None:
        self._timeout = timeout
        self._headers = {
            "User-Agent": "pi-invest-agent/0.1 (+https://github.com/local)"
        }

    def get_history(self, symbol: str, lookback_days: int = 60) -> list[Bar]:
        params = {
            "range": f"{max(lookback_days, 5)}d",
            "interval": "1d",
            "includePrePost": "false",
        }
        url = self.CHART.format(symbol=symbol.upper())
        with httpx.Client(timeout=self._timeout, headers=self._headers) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            payload = resp.json()
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            raise RuntimeError(f"No Yahoo chart data for {symbol}")
        meta = result[0]
        timestamps = meta.get("timestamp") or []
        quote = (meta.get("indicators") or {}).get("quote") or [{}]
        q0 = quote[0]
        bars: list[Bar] = []
        for i, ts in enumerate(timestamps):
            close = (q0.get("close") or [None])[i]
            if close is None:
                continue
            open_px = (q0.get("open") or [close])[i] or close
            high = (q0.get("high") or [close])[i] or close
            low = (q0.get("low") or [close])[i] or close
            vol = (q0.get("volume") or [0])[i] or 0
            bars.append(
                Bar(
                    symbol=symbol.upper(),
                    ts=datetime.fromtimestamp(ts, tz=timezone.utc),
                    open=float(open_px),
                    high=float(high),
                    low=float(low),
                    close=float(close),
                    volume=float(vol),
                )
            )
        return bars

    def get_quote(self, symbol: str) -> Quote:
        bars = self.get_history(symbol, lookback_days=5)
        if not bars:
            raise RuntimeError(f"No quote for {symbol}")
        last = bars[-1]
        prev = bars[-2].close if len(bars) > 1 else last.close
        # Yahoo chart meta sometimes has dividend yield via separate call; leave None
        return Quote(
            symbol=symbol.upper(),
            price=last.close,
            previous_close=prev,
            day_high=last.high,
            day_low=last.low,
            volume=last.volume,
            dividend_yield=_SIM_BASE.get(symbol.upper(), (0.0, 0.0))[1] or None,
        )


class ResilientMarketData:
    """Prefer live provider; fall back to simulator when configured."""

    def __init__(
        self,
        primary: MarketDataProvider,
        fallback: MarketDataProvider | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.last_source: str = "primary"

    def get_quote(self, symbol: str) -> Quote:
        try:
            q = self.primary.get_quote(symbol)
            self.last_source = "primary"
            return q
        except Exception:
            if self.fallback is None:
                raise
            self.last_source = "simulator"
            return self.fallback.get_quote(symbol)

    def get_history(self, symbol: str, lookback_days: int = 60) -> list[Bar]:
        try:
            h = self.primary.get_history(symbol, lookback_days=lookback_days)
            if len(h) < 5:
                raise RuntimeError("insufficient history")
            self.last_source = "primary"
            return h
        except Exception:
            if self.fallback is None:
                raise
            self.last_source = "simulator"
            return self.fallback.get_history(symbol, lookback_days=lookback_days)


def returns(closes: list[float]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(closes)):
        if closes[i - 1] == 0:
            out.append(0.0)
        else:
            out.append((closes[i] - closes[i - 1]) / closes[i - 1])
    return out


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def build_market_data(
    provider: str,
    allow_simulator_fallback: bool = True,
) -> ResilientMarketData:
    sim = SimulatorMarketData()
    if provider == "simulator":
        return ResilientMarketData(sim, None)
    yahoo = YahooMarketData()
    return ResilientMarketData(yahoo, sim if allow_simulator_fallback else None)
