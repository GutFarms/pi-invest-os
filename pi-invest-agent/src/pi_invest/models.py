from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class Quote(BaseModel):
    symbol: str
    price: float
    previous_close: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    volume: float | None = None
    dividend_yield: float | None = None
    timestamp: datetime = Field(default_factory=utcnow)


class Bar(BaseModel):
    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class Position(BaseModel):
    symbol: str
    qty: float
    avg_cost: float
    market_price: float = 0.0

    @property
    def market_value(self) -> float:
        return self.qty * self.market_price

    @property
    def cost_basis(self) -> float:
        return self.qty * self.avg_cost

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_basis


class AccountSnapshot(BaseModel):
    cash: float
    equity: float
    buying_power: float
    positions: list[Position] = Field(default_factory=list)
    day_pnl: float = 0.0
    day_pnl_pct: float = 0.0


class OrderRequest(BaseModel):
    symbol: str
    side: Side
    notional: float | None = None
    qty: float | None = None
    reason: str = ""
    confidence: float = 0.0


class OrderResult(BaseModel):
    ok: bool
    order_id: str = ""
    symbol: str
    side: Side
    qty: float = 0.0
    fill_price: float = 0.0
    message: str = ""
    paper: bool = True
    timestamp: datetime = Field(default_factory=utcnow)


class SignalScore(BaseModel):
    symbol: str
    momentum: float
    yield_score: float
    trend: float
    volatility_penalty: float
    composite: float
    expected_income_proxy: float
    notes: list[str] = Field(default_factory=list)


class TradeIntent(BaseModel):
    symbol: str
    side: Side
    target_weight: float = 0.0
    confidence: float = 0.0
    rationale: str = ""
    source: str = "heuristic"


class Decision(BaseModel):
    cycle_id: str
    timestamp: datetime = Field(default_factory=utcnow)
    market_open: bool = True
    scores: list[SignalScore] = Field(default_factory=list)
    intents: list[TradeIntent] = Field(default_factory=list)
    orders: list[OrderResult] = Field(default_factory=list)
    skipped_reasons: list[str] = Field(default_factory=list)
    llm_raw: str | None = None
    account_after: AccountSnapshot | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class TransferDirection(str, Enum):
    SEND = "send"
    RECEIVE = "receive"


class TransferStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class AssetBalance(BaseModel):
    asset: str
    amount: float
    available: float
    usd_mark: float | None = None

    @property
    def usd_value(self) -> float:
        return self.amount * (self.usd_mark or 0.0)


class ReceiveAddress(BaseModel):
    asset: str
    address: str
    network: str = "paper"
    memo_tag: str | None = None


class TransferRecord(BaseModel):
    transfer_id: str
    direction: TransferDirection
    asset: str
    amount: float
    fee: float = 0.0
    counterparty: str
    network: str = "paper"
    status: TransferStatus = TransferStatus.COMPLETED
    memo: str = ""
    paper: bool = True
    tx_ref: str = ""
    timestamp: datetime = Field(default_factory=utcnow)


class WalletSnapshot(BaseModel):
    backend: str
    balances: list[AssetBalance] = Field(default_factory=list)
    addresses: dict[str, ReceiveAddress] = Field(default_factory=dict)
    total_usd_estimate: float = 0.0
    meta: dict[str, Any] = Field(default_factory=dict)


class HaltState(BaseModel):
    halted: bool = False
    reason: str = ""
    updated_at: datetime | None = None


class EquitySnapshot(BaseModel):
    snapshot_id: str
    timestamp: datetime = Field(default_factory=utcnow)
    equity: float
    cash: float
    wallet_usd: float
    total_nav: float
    day_pnl: float = 0.0
    day_pnl_pct: float = 0.0
    peak_nav: float = 0.0
    drawdown_pct: float = 0.0
    halted: bool = False
    cycle_id: str | None = None


class JournalSummary(BaseModel):
    points: int = 0
    start_nav: float | None = None
    latest_nav: float | None = None
    peak_nav: float | None = None
    max_drawdown_pct: float = 0.0
    total_return_pct: float | None = None
    latest: EquitySnapshot | None = None
    halted: bool = False
    halt_reason: str = ""

