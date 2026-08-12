from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from pi_invest.models import (
    Decision,
    ReceiveAddress,
    Side,
    TransferDirection,
    TransferRecord,
    TransferStatus,
    utcnow,
)


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cash REAL NOT NULL,
                    day_start_equity REAL NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_positions (
                    symbol TEXT PRIMARY KEY,
                    qty REAL NOT NULL,
                    avg_cost REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL NOT NULL,
                    fill_price REAL NOT NULL,
                    paper INTEGER NOT NULL,
                    reason TEXT,
                    confidence REAL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    cycle_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wallet_balances (
                    asset TEXT PRIMARY KEY,
                    amount REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wallet_addresses (
                    asset TEXT PRIMARY KEY,
                    address TEXT NOT NULL,
                    network TEXT NOT NULL,
                    memo_tag TEXT
                );
                CREATE TABLE IF NOT EXISTS wallet_transfers (
                    transfer_id TEXT PRIMARY KEY,
                    direction TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    amount REAL NOT NULL,
                    fee REAL NOT NULL,
                    counterparty TEXT NOT NULL,
                    network TEXT NOT NULL,
                    status TEXT NOT NULL,
                    memo TEXT,
                    paper INTEGER NOT NULL,
                    tx_ref TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS withdrawal_allowlist (
                    destination TEXT PRIMARY KEY,
                    label TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    halted INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    equity REAL NOT NULL,
                    cash REAL NOT NULL,
                    wallet_usd REAL NOT NULL,
                    total_nav REAL NOT NULL,
                    day_pnl REAL NOT NULL,
                    day_pnl_pct REAL NOT NULL,
                    peak_nav REAL NOT NULL,
                    drawdown_pct REAL NOT NULL,
                    halted INTEGER NOT NULL,
                    cycle_id TEXT
                );
                CREATE TABLE IF NOT EXISTS kv_store (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            row = conn.execute("SELECT id FROM agent_state WHERE id = 1").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO agent_state (id, halted, reason, updated_at) "
                    "VALUES (1, 0, '', ?)",
                    (utcnow().isoformat(),),
                )

    def ensure_paper_account(self, starting_cash: float) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM paper_account WHERE id = 1").fetchone()
            if row is None:
                now = utcnow().isoformat()
                conn.execute(
                    "INSERT INTO paper_account (id, cash, day_start_equity, updated_at) "
                    "VALUES (1, ?, ?, ?)",
                    (starting_cash, starting_cash, now),
                )

    def reset_paper(self, starting_cash: float) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM paper_positions")
            conn.execute("DELETE FROM paper_account")
            now = utcnow().isoformat()
            conn.execute(
                "INSERT INTO paper_account (id, cash, day_start_equity, updated_at) "
                "VALUES (1, ?, ?, ?)",
                (starting_cash, starting_cash, now),
            )

    def load_paper_state(self) -> tuple[float, list[tuple[str, float, float]], float]:
        with self._connect() as conn:
            acct = conn.execute(
                "SELECT cash, day_start_equity FROM paper_account WHERE id = 1"
            ).fetchone()
            if acct is None:
                return 0.0, [], 0.0
            positions = [
                (r["symbol"], r["qty"], r["avg_cost"])
                for r in conn.execute(
                    "SELECT symbol, qty, avg_cost FROM paper_positions WHERE qty > 0"
                )
            ]
            return float(acct["cash"]), positions, float(acct["day_start_equity"])

    def apply_paper_fill(
        self, symbol: str, side: Side, qty: float, price: float
    ) -> None:
        symbol = symbol.upper()
        with self._connect() as conn:
            cash = float(
                conn.execute("SELECT cash FROM paper_account WHERE id = 1").fetchone()[
                    "cash"
                ]
            )
            row = conn.execute(
                "SELECT qty, avg_cost FROM paper_positions WHERE symbol = ?",
                (symbol,),
            ).fetchone()
            held_qty = float(row["qty"]) if row else 0.0
            avg_cost = float(row["avg_cost"]) if row else 0.0

            if side == Side.BUY:
                new_qty = held_qty + qty
                new_avg = (
                    ((held_qty * avg_cost) + (qty * price)) / new_qty if new_qty else 0.0
                )
                cash -= qty * price
                conn.execute(
                    """
                    INSERT INTO paper_positions (symbol, qty, avg_cost)
                    VALUES (?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                      qty=excluded.qty, avg_cost=excluded.avg_cost
                    """,
                    (symbol, new_qty, new_avg),
                )
            else:
                new_qty = held_qty - qty
                cash += qty * price
                if new_qty <= 1e-9:
                    conn.execute(
                        "DELETE FROM paper_positions WHERE symbol = ?", (symbol,)
                    )
                else:
                    conn.execute(
                        "UPDATE paper_positions SET qty = ? WHERE symbol = ?",
                        (new_qty, symbol),
                    )

            conn.execute(
                "UPDATE paper_account SET cash = ?, updated_at = ? WHERE id = 1",
                (cash, utcnow().isoformat()),
            )

    def log_order(
        self,
        order_id: str,
        symbol: str,
        side: str,
        qty: float,
        fill_price: float,
        paper: bool,
        reason: str,
        confidence: float,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO orders (
                  order_id, symbol, side, qty, fill_price, paper, reason, confidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    symbol,
                    side,
                    qty,
                    fill_price,
                    1 if paper else 0,
                    reason,
                    confidence,
                    utcnow().isoformat(),
                ),
            )

    def save_decision(self, decision: Decision) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO decisions (cycle_id, created_at, payload) "
                "VALUES (?, ?, ?)",
                (
                    decision.cycle_id,
                    decision.timestamp.isoformat(),
                    decision.model_dump_json(),
                ),
            )

    def recent_decisions(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM decisions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [json.loads(r["payload"]) for r in rows]

    def recent_orders(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT order_id, symbol, side, qty, fill_price, paper, reason,
                       confidence, created_at
                FROM orders ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def rollover_day_start_if_needed(self, equity: float, force: bool = False) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT day_start_equity, updated_at FROM paper_account WHERE id = 1"
            ).fetchone()
            if row is None:
                return
            updated = row["updated_at"]
            today = utcnow().date().isoformat()
            if force or not str(updated).startswith(today):
                conn.execute(
                    "UPDATE paper_account SET day_start_equity = ?, updated_at = ? "
                    "WHERE id = 1",
                    (equity, utcnow().isoformat()),
                )

    def set_paper_cash(self, cash: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE paper_account SET cash = ?, updated_at = ? WHERE id = 1",
                (cash, utcnow().isoformat()),
            )

    def ensure_wallet(
        self,
        starting: dict[str, float],
        assets: list[str],
        address_fn,
        networks: dict[str, str],
    ) -> None:
        with self._connect() as conn:
            for asset in assets:
                a = asset.upper()
                row = conn.execute(
                    "SELECT asset FROM wallet_balances WHERE asset = ?", (a,)
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO wallet_balances (asset, amount) VALUES (?, ?)",
                        (a, float(starting.get(a, starting.get(asset, 0.0)))),
                    )
                addr_row = conn.execute(
                    "SELECT asset FROM wallet_addresses WHERE asset = ?", (a,)
                ).fetchone()
                if addr_row is None:
                    conn.execute(
                        "INSERT INTO wallet_addresses (asset, address, network, memo_tag) "
                        "VALUES (?, ?, ?, NULL)",
                        (a, address_fn(a), networks.get(a, "paper")),
                    )

    def wallet_balances(self) -> dict[str, float]:
        with self._connect() as conn:
            rows = conn.execute("SELECT asset, amount FROM wallet_balances").fetchall()
            return {r["asset"]: float(r["amount"]) for r in rows}

    def adjust_wallet_balance(self, asset: str, delta: float) -> float:
        asset = asset.upper()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT amount FROM wallet_balances WHERE asset = ?", (asset,)
            ).fetchone()
            current = float(row["amount"]) if row else 0.0
            new_amt = current + delta
            if new_amt < -1e-9:
                raise ValueError(f"wallet {asset} would go negative ({new_amt})")
            if row is None:
                conn.execute(
                    "INSERT INTO wallet_balances (asset, amount) VALUES (?, ?)",
                    (asset, max(0.0, new_amt)),
                )
            else:
                conn.execute(
                    "UPDATE wallet_balances SET amount = ? WHERE asset = ?",
                    (max(0.0, new_amt), asset),
                )
            return max(0.0, new_amt)

    def wallet_addresses(self) -> list[ReceiveAddress]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT asset, address, network, memo_tag FROM wallet_addresses"
            ).fetchall()
            return [
                ReceiveAddress(
                    asset=r["asset"],
                    address=r["address"],
                    network=r["network"],
                    memo_tag=r["memo_tag"],
                )
                for r in rows
            ]

    def get_or_create_address(
        self, asset: str, address: str, network: str
    ) -> ReceiveAddress:
        asset = asset.upper()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT asset, address, network, memo_tag FROM wallet_addresses "
                "WHERE asset = ?",
                (asset,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO wallet_addresses (asset, address, network, memo_tag) "
                    "VALUES (?, ?, ?, NULL)",
                    (asset, address, network),
                )
                return ReceiveAddress(asset=asset, address=address, network=network)
            return ReceiveAddress(
                asset=row["asset"],
                address=row["address"],
                network=row["network"],
                memo_tag=row["memo_tag"],
            )

    def upsert_wallet_address(
        self, asset: str, address: str, network: str
    ) -> ReceiveAddress:
        asset = asset.upper()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO wallet_addresses (asset, address, network, memo_tag)
                VALUES (?, ?, ?, NULL)
                ON CONFLICT(asset) DO UPDATE SET
                  address=excluded.address,
                  network=excluded.network
                """,
                (asset, address, network),
            )
        return ReceiveAddress(asset=asset, address=address, network=network)

    def save_transfer(self, record: TransferRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO wallet_transfers (
                  transfer_id, direction, asset, amount, fee, counterparty,
                  network, status, memo, paper, tx_ref, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.transfer_id,
                    record.direction.value,
                    record.asset,
                    record.amount,
                    record.fee,
                    record.counterparty,
                    record.network,
                    record.status.value,
                    record.memo,
                    1 if record.paper else 0,
                    record.tx_ref,
                    record.timestamp.isoformat(),
                ),
            )

    def list_transfers(self, limit: int = 25) -> list[TransferRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT transfer_id, direction, asset, amount, fee, counterparty,
                       network, status, memo, paper, tx_ref, created_at
                FROM wallet_transfers
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            out: list[TransferRecord] = []
            for r in rows:
                ts_raw = r["created_at"]
                try:
                    ts = datetime.fromisoformat(ts_raw) if ts_raw else utcnow()
                except ValueError:
                    ts = utcnow()
                out.append(
                    TransferRecord(
                        transfer_id=r["transfer_id"],
                        direction=TransferDirection(r["direction"]),
                        asset=r["asset"],
                        amount=float(r["amount"]),
                        fee=float(r["fee"]),
                        counterparty=r["counterparty"],
                        network=r["network"],
                        status=TransferStatus(r["status"]),
                        memo=r["memo"] or "",
                        paper=bool(r["paper"]),
                        tx_ref=r["tx_ref"] or "",
                        timestamp=ts,
                    )
                )
            return out

    def reset_wallet(self, starting: dict[str, float]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM wallet_transfers")
            for asset, amount in starting.items():
                conn.execute(
                    """
                    INSERT INTO wallet_balances (asset, amount) VALUES (?, ?)
                    ON CONFLICT(asset) DO UPDATE SET amount=excluded.amount
                    """,
                    (asset.upper(), float(amount)),
                )

    def get_halt_state(self) -> tuple[bool, str, str | None]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT halted, reason, updated_at FROM agent_state WHERE id = 1"
            ).fetchone()
            if row is None:
                return False, "", None
            return bool(row["halted"]), str(row["reason"] or ""), row["updated_at"]

    def set_halt_state(self, halted: bool, reason: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_state (id, halted, reason, updated_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  halted=excluded.halted,
                  reason=excluded.reason,
                  updated_at=excluded.updated_at
                """,
                (1 if halted else 0, reason, utcnow().isoformat()),
            )

    def save_equity_snapshot(self, snap) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO equity_snapshots (
                  snapshot_id, created_at, equity, cash, wallet_usd, total_nav,
                  day_pnl, day_pnl_pct, peak_nav, drawdown_pct, halted, cycle_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snap.snapshot_id,
                    snap.timestamp.isoformat(),
                    snap.equity,
                    snap.cash,
                    snap.wallet_usd,
                    snap.total_nav,
                    snap.day_pnl,
                    snap.day_pnl_pct,
                    snap.peak_nav,
                    snap.drawdown_pct,
                    1 if snap.halted else 0,
                    snap.cycle_id,
                ),
            )

    def list_equity_snapshots(self, limit: int = 100) -> list:
        from pi_invest.models import EquitySnapshot

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT snapshot_id, created_at, equity, cash, wallet_usd, total_nav,
                       day_pnl, day_pnl_pct, peak_nav, drawdown_pct, halted, cycle_id
                FROM equity_snapshots
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            try:
                ts = datetime.fromisoformat(r["created_at"])
            except ValueError:
                ts = utcnow()
            out.append(
                EquitySnapshot(
                    snapshot_id=r["snapshot_id"],
                    timestamp=ts,
                    equity=float(r["equity"]),
                    cash=float(r["cash"]),
                    wallet_usd=float(r["wallet_usd"]),
                    total_nav=float(r["total_nav"]),
                    day_pnl=float(r["day_pnl"]),
                    day_pnl_pct=float(r["day_pnl_pct"]),
                    peak_nav=float(r["peak_nav"]),
                    drawdown_pct=float(r["drawdown_pct"]),
                    halted=bool(r["halted"]),
                    cycle_id=r["cycle_id"],
                )
            )
        return out

    def peak_nav(self) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(total_nav) AS peak FROM equity_snapshots"
            ).fetchone()
            if row is None or row["peak"] is None:
                return 0.0
            return float(row["peak"])

    def outbound_send_usd_today(self, marks: dict[str, float]) -> float:
        """Sum completed outbound sends today, valued in USD using marks."""
        today = utcnow().date().isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT asset, amount, network, created_at
                FROM wallet_transfers
                WHERE direction = 'send' AND status = 'completed'
                  AND created_at LIKE ?
                """,
                (f"{today}%",),
            ).fetchall()
        total = 0.0
        for r in rows:
            asset = r["asset"]
            px = marks.get(asset, 1.0 if asset in {"USD", "USDC", "USDT"} else 0.0)
            total += float(r["amount"]) * px
        return total

    def ensure_allowlist(self, bootstrap: list[str]) -> None:
        with self._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM withdrawal_allowlist"
            ).fetchone()["c"]
            if count == 0 and bootstrap:
                now = utcnow().isoformat()
                for dest in bootstrap:
                    d = dest.strip()
                    if not d:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO withdrawal_allowlist "
                        "(destination, label, created_at) VALUES (?, ?, ?)",
                        (d, "", now),
                    )

    def list_allowlist(self) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT destination, label, created_at FROM withdrawal_allowlist "
                "ORDER BY created_at ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def add_allowlist(self, destination: str, label: str = "") -> None:
        dest = destination.strip()
        if not dest:
            raise ValueError("destination required")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO withdrawal_allowlist (destination, label, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(destination) DO UPDATE SET label=excluded.label
                """,
                (dest, label.strip(), utcnow().isoformat()),
            )

    def remove_allowlist(self, destination: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM withdrawal_allowlist WHERE destination = ?",
                (destination.strip(),),
            )
            return cur.rowcount > 0

    def is_allowlisted(self, destination: str) -> bool:
        dest = destination.strip()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM withdrawal_allowlist WHERE destination = ?",
                (dest,),
            ).fetchone()
            if row:
                return True
            # Case-insensitive match for emails / tags
            row = conn.execute(
                "SELECT 1 FROM withdrawal_allowlist WHERE lower(destination) = lower(?)",
                (dest,),
            ).fetchone()
            return row is not None

    def audit(self, kind: str, detail: str) -> None:
        import uuid as _uuid

        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_events (event_id, kind, detail, created_at) "
                "VALUES (?, ?, ?, ?)",
                (str(_uuid.uuid4()), kind, detail, utcnow().isoformat()),
            )

    def recent_audit(self, limit: int = 30) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT event_id, kind, detail, created_at FROM audit_events "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM kv_store WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return default
            return str(row["value"])

    def kv_set(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO kv_store (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )

    def get_alert_drawdown_peak(self) -> float:
        raw = self.kv_get("alert.drawdown_peak")
        if raw is None:
            return 0.0
        try:
            return float(raw)
        except ValueError:
            return 0.0

    def set_alert_drawdown_peak(self, peak: float) -> None:
        self.kv_set("alert.drawdown_peak", str(peak))

