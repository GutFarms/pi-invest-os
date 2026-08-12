from __future__ import annotations

import hashlib
import secrets
import uuid
from abc import ABC, abstractmethod

from pi_invest.config import AppConfig, EnvSettings, WalletConfig
from pi_invest.models import (
    AssetBalance,
    ReceiveAddress,
    TransferDirection,
    TransferRecord,
    TransferStatus,
    WalletSnapshot,
)
from pi_invest.storage.db import Database
from pi_invest.wallet.coinbase_client import CoinbaseAPIError, CoinbaseClient


# Approximate USD marks for paper display
DEFAULT_MARKS_USD: dict[str, float] = {
    "USD": 1.0,
    "USDC": 1.0,
    "USDT": 1.0,
    "BTC": 95_000.0,
    "ETH": 3_500.0,
    "SOL": 180.0,
}


class WalletError(Exception):
    pass


class WalletBackend(ABC):
    @abstractmethod
    def snapshot(self) -> WalletSnapshot: ...

    @abstractmethod
    def receive_address(self, asset: str, network: str | None = None) -> ReceiveAddress: ...

    @abstractmethod
    def send(
        self,
        asset: str,
        amount: float,
        to_address: str,
        memo: str = "",
        network: str | None = None,
    ) -> TransferRecord: ...

    @abstractmethod
    def credit_inbound(
        self,
        asset: str,
        amount: float,
        from_address: str = "external",
        memo: str = "",
    ) -> TransferRecord: ...

    @abstractmethod
    def history(self, limit: int = 25) -> list[TransferRecord]: ...


def _device_suffix(db_path: str) -> str:
    return hashlib.sha256(db_path.encode()).hexdigest()[:10]


def _paper_address(asset: str, suffix: str, network: str) -> str:
    asset = asset.upper()
    if asset == "USD":
        return f"usd:piinvest:{suffix}"
    if asset in {"USDC", "USDT"}:
        return f"0xpaper{suffix}{asset.lower()}"
    if asset == "BTC":
        return f"paper-btc-{suffix}"
    if asset == "ETH":
        return f"0xpaper{suffix}"
    if asset == "SOL":
        return f"paper-sol-{suffix}"
    return f"paper-{asset.lower()}-{suffix}"


class PaperWallet(WalletBackend):
    """Local fiat + crypto treasury with simulated send/receive."""

    def __init__(self, db: Database, cfg: WalletConfig) -> None:
        self.db = db
        self.cfg = cfg
        self.suffix = _device_suffix(str(db.path))
        self.db.ensure_wallet(
            starting=cfg.starting_balances,
            assets=cfg.assets,
            address_fn=lambda asset: _paper_address(
                asset,
                self.suffix,
                cfg.networks.get(asset.upper(), "paper"),
            ),
            networks=cfg.networks,
        )

    def snapshot(self) -> WalletSnapshot:
        balances_raw = self.db.wallet_balances()
        addresses = {a.asset: a for a in self.db.wallet_addresses()}
        balances = [
            AssetBalance(
                asset=asset,
                amount=amount,
                available=amount,
                usd_mark=DEFAULT_MARKS_USD.get(asset, 0.0),
            )
            for asset, amount in balances_raw.items()
        ]
        total = sum(b.amount * (b.usd_mark or 0.0) for b in balances)
        return WalletSnapshot(
            backend="paper",
            balances=balances,
            addresses=addresses,
            total_usd_estimate=round(total, 2),
        )

    def receive_address(self, asset: str, network: str | None = None) -> ReceiveAddress:
        asset = asset.upper()
        if asset not in {a.upper() for a in self.cfg.assets}:
            raise WalletError(f"{asset} is not an enabled wallet asset")
        return self.db.get_or_create_address(
            asset,
            _paper_address(
                asset,
                self.suffix,
                network or self.cfg.networks.get(asset, "paper"),
            ),
            network or self.cfg.networks.get(asset, "paper"),
        )

    def send(
        self,
        asset: str,
        amount: float,
        to_address: str,
        memo: str = "",
        network: str | None = None,
    ) -> TransferRecord:
        asset = asset.upper()
        if amount <= 0:
            raise WalletError("amount must be positive")
        if not to_address.strip():
            raise WalletError("destination required")
        if amount < self.cfg.min_send.get(asset, 0.0):
            raise WalletError(
                f"amount below minimum send for {asset} "
                f"({self.cfg.min_send.get(asset, 0.0)})"
            )

        bal = self.db.wallet_balances().get(asset, 0.0)
        fee = self.cfg.send_fee.get(asset, 0.0)
        total = amount + fee
        if total > bal + 1e-12:
            raise WalletError(
                f"insufficient {asset}: have {bal}, need {total} (incl. fee {fee})"
            )

        self.db.adjust_wallet_balance(asset, -total)
        record = TransferRecord(
            transfer_id=str(uuid.uuid4()),
            direction=TransferDirection.SEND,
            asset=asset,
            amount=amount,
            fee=fee,
            counterparty=to_address.strip(),
            network=network or self.cfg.networks.get(asset, "paper"),
            status=TransferStatus.COMPLETED,
            memo=memo,
            paper=True,
            tx_ref=f"paper-{secrets.token_hex(8)}",
        )
        self.db.save_transfer(record)
        return record

    def credit_inbound(
        self,
        asset: str,
        amount: float,
        from_address: str = "external",
        memo: str = "",
    ) -> TransferRecord:
        asset = asset.upper()
        if amount <= 0:
            raise WalletError("amount must be positive")
        if asset not in {a.upper() for a in self.cfg.assets}:
            raise WalletError(f"{asset} is not an enabled wallet asset")
        self.db.adjust_wallet_balance(asset, amount)
        record = TransferRecord(
            transfer_id=str(uuid.uuid4()),
            direction=TransferDirection.RECEIVE,
            asset=asset,
            amount=amount,
            fee=0.0,
            counterparty=from_address,
            network=self.cfg.networks.get(asset, "paper"),
            status=TransferStatus.COMPLETED,
            memo=memo or "inbound credit",
            paper=True,
            tx_ref=f"paper-in-{secrets.token_hex(8)}",
        )
        self.db.save_transfer(record)
        return record

    def history(self, limit: int = 25) -> list[TransferRecord]:
        return self.db.list_transfers(limit=limit)


class CoinbaseWallet(WalletBackend):
    """
    Live Coinbase App connection via CDP JWT.

    - Read (balances, receive addresses): needs COINBASE_API_KEY + SECRET
    - Send: also needs ALLOW_LIVE_TRANSFERS=true (and safety gate / daily caps)
    """

    def __init__(self, env: EnvSettings, cfg: WalletConfig, db: Database) -> None:
        if not env.coinbase_api_key or not env.coinbase_api_secret:
            raise WalletError(
                "Coinbase backend requires COINBASE_API_KEY and COINBASE_API_SECRET"
            )
        self.env = env
        self.cfg = cfg
        self.db = db
        self.client = CoinbaseClient(env)
        # Keep a local mirror for history / offline fallback notes
        self._paper = PaperWallet(db, cfg)
        self._account_cache: dict[str, str] = {}

    def ping(self) -> dict:
        return self.client.ping()

    def snapshot(self) -> WalletSnapshot:
        try:
            accounts = self.client.list_app_accounts()
        except CoinbaseAPIError as exc:
            snap = self._paper.snapshot()
            snap.backend = "coinbase-error"
            snap.meta = {"error": str(exc)}
            return snap

        wanted = {a.upper() for a in self.cfg.assets}
        balances: list[AssetBalance] = []
        addresses: dict[str, ReceiveAddress] = {}
        by_ccy: dict[str, float] = {}
        for acct in accounts:
            if wanted and acct.currency not in wanted:
                # Always include USD if present even if not listed? stick to config
                continue
            by_ccy[acct.currency] = by_ccy.get(acct.currency, 0.0) + acct.balance
            self._account_cache[acct.currency] = acct.id

        for asset in self.cfg.assets:
            ccy = asset.upper()
            amount = by_ccy.get(ccy, 0.0)
            balances.append(
                AssetBalance(
                    asset=ccy,
                    amount=amount,
                    available=amount,
                    usd_mark=DEFAULT_MARKS_USD.get(ccy, 0.0),
                )
            )
            # Prefer cached receive address from DB, else fetch lazily on demand
            try:
                local = self.db.get_or_create_address(
                    ccy,
                    _paper_address(ccy, _device_suffix(str(self.db.path)), "coinbase"),
                    self.cfg.networks.get(ccy, "coinbase"),
                )
                # If we previously stored a real address, use it
                if local.address and not local.address.startswith(
                    ("paper-", "0xpaper", "usd:piinvest:")
                ):
                    addresses[ccy] = local
            except Exception:  # noqa: BLE001
                pass

        total = sum(b.amount * (b.usd_mark or 0.0) for b in balances)
        return WalletSnapshot(
            backend="coinbase",
            balances=balances,
            addresses=addresses,
            total_usd_estimate=round(total, 2),
            meta={
                "accounts_seen": len(accounts),
                "live_transfers": self.env.allow_live_transfers,
            },
        )

    def receive_address(self, asset: str, network: str | None = None) -> ReceiveAddress:
        asset = asset.upper()
        try:
            cb_addr = self.client.get_or_create_receive_address(asset)
        except CoinbaseAPIError as exc:
            raise WalletError(str(exc)) from exc
        net = network or cb_addr.network or self.cfg.networks.get(asset, "coinbase")
        return self.db.upsert_wallet_address(asset, cb_addr.address, net)

    def send(
        self,
        asset: str,
        amount: float,
        to_address: str,
        memo: str = "",
        network: str | None = None,
    ) -> TransferRecord:
        if not self.env.allow_live_transfers:
            raise WalletError(
                "Live Coinbase sends require ALLOW_LIVE_TRANSFERS=true in .env"
            )
        asset = asset.upper()
        if amount <= 0:
            raise WalletError("amount must be positive")
        if not to_address.strip():
            raise WalletError("destination required")
        try:
            raw = self.client.send_money(
                asset,
                amount,
                to_address.strip(),
                description=memo,
                network=network,
                destination_tag=memo if memo and asset in {"XRP", "XLM", "HBAR"} else None,
            )
        except CoinbaseAPIError as exc:
            raise WalletError(str(exc)) from exc

        tx_id = str(raw.get("id") or raw.get("resource_path") or secrets.token_hex(8))
        status_raw = str(raw.get("status") or "pending").lower()
        status = (
            TransferStatus.COMPLETED
            if status_raw in {"completed", "complete"}
            else TransferStatus.PENDING
        )
        record = TransferRecord(
            transfer_id=str(uuid.uuid4()),
            direction=TransferDirection.SEND,
            asset=asset,
            amount=amount,
            fee=0.0,
            counterparty=to_address.strip(),
            network=network or self.cfg.networks.get(asset, "coinbase"),
            status=status,
            memo=memo,
            paper=False,
            tx_ref=tx_id,
        )
        self.db.save_transfer(record)
        return record

    def credit_inbound(
        self,
        asset: str,
        amount: float,
        from_address: str = "external",
        memo: str = "",
    ) -> TransferRecord:
        # Live inbound is detected on Coinbase; local credit is bookkeeping only
        return self._paper.credit_inbound(asset, amount, from_address, memo)

    def history(self, limit: int = 25) -> list[TransferRecord]:
        # Prefer local log (includes paper + live sends we initiated)
        return self.db.list_transfers(limit=limit)


class WalletService:
    """High-level wallet ops including USD bridge to paper brokerage cash."""

    def __init__(
        self,
        backend: WalletBackend,
        db: Database,
        cfg: WalletConfig,
        env: EnvSettings,
        safety: "SafetyGate | None" = None,
    ) -> None:
        self.backend = backend
        self.db = db
        self.cfg = cfg
        self.env = env
        self.safety = safety

    def snapshot(self) -> WalletSnapshot:
        return self.backend.snapshot()

    def receive_info(self, asset: str) -> ReceiveAddress:
        return self.backend.receive_address(asset)

    def _usd_value(self, asset: str, amount: float) -> float:
        mark = DEFAULT_MARKS_USD.get(asset.upper(), 0.0)
        return amount * mark

    def _assert_can_send(
        self,
        asset: str,
        amount: float,
        to_address: str | None = None,
        *,
        confirm: str | None = None,
        internal: bool = False,
    ) -> None:
        if self.safety is not None:
            try:
                self.safety.assert_send_allowed()
            except Exception as exc:  # HaltedError
                raise WalletError(str(exc)) from exc
        usd = self._usd_value(asset, amount)
        spent = self.db.outbound_send_usd_today(DEFAULT_MARKS_USD)
        cap = self.cfg.max_daily_send_usd
        if spent + usd > cap + 1e-9:
            raise WalletError(
                f"daily send limit exceeded: ${spent:.2f} + ${usd:.2f} > ${cap:.2f}"
            )
        if not internal:
            if self.cfg.require_send_confirmation:
                from pi_invest.wallet.confirm import (
                    confirmation_phrase,
                    confirmations_match,
                )

                expected = confirmation_phrase(asset, amount)
                if not confirmations_match(expected, confirm):
                    raise WalletError(
                        f"confirmation required — type exactly: {expected}"
                    )
            if self.cfg.allowlist_required:
                if not to_address:
                    raise WalletError("destination required")
                if not self.db.is_allowlisted(to_address):
                    raise WalletError(
                        f"destination not on withdrawal allowlist: {to_address}. "
                        "Add it with `pi-invest wallet allowlist-add` first."
                    )

    def send(
        self,
        asset: str,
        amount: float,
        to_address: str,
        memo: str = "",
        network: str | None = None,
        confirm: str | None = None,
    ) -> TransferRecord:
        self._assert_can_send(asset, amount, to_address, confirm=confirm, internal=False)
        record = self.backend.send(
            asset, amount, to_address, memo=memo, network=network
        )
        try:
            self.db.audit(
                "wallet.send",
                f"{record.direction.value} {record.amount} {record.asset} "
                f"-> {record.counterparty} ref={record.tx_ref}",
            )
        except Exception:  # noqa: BLE001
            pass
        return record

    def receive(
        self,
        asset: str,
        amount: float,
        from_address: str = "external",
        memo: str = "",
    ) -> TransferRecord:
        # Inbound receives remain allowed during halt
        return self.backend.credit_inbound(asset, amount, from_address, memo)

    def history(self, limit: int = 25) -> list[TransferRecord]:
        return self.backend.history(limit=limit)

    def bridge_to_broker(self, amount_usd: float, confirm: str | None = None) -> TransferRecord:
        if amount_usd <= 0:
            raise WalletError("amount must be positive")
        if self.cfg.require_send_confirmation:
            from pi_invest.wallet.confirm import (
                bridge_phrase,
                confirmation_phrase,
                confirmations_match,
            )

            expected_send = confirmation_phrase("USD", amount_usd)
            expected_bridge = bridge_phrase(amount_usd)
            if not (
                confirmations_match(expected_send, confirm)
                or confirmations_match(expected_bridge, confirm)
            ):
                raise WalletError(
                    f"confirmation required — type exactly: {expected_bridge}"
                )
        self._assert_can_send("USD", amount_usd, internal=True)
        bal = self.db.wallet_balances().get("USD", 0.0)
        if amount_usd > bal + 1e-9:
            raise WalletError(f"insufficient wallet USD ({bal})")
        self.db.ensure_paper_account(0.0)
        cash, _, _ = self.db.load_paper_state()
        self.db.adjust_wallet_balance("USD", -amount_usd)
        self.db.set_paper_cash(cash + amount_usd)
        record = TransferRecord(
            transfer_id=str(uuid.uuid4()),
            direction=TransferDirection.SEND,
            asset="USD",
            amount=amount_usd,
            counterparty="broker:paper",
            network="internal",
            status=TransferStatus.COMPLETED,
            memo="bridge wallet → brokerage cash",
            paper=True,
            tx_ref=f"bridge-out-{secrets.token_hex(6)}",
        )
        self.db.save_transfer(record)
        self.db.audit("wallet.bridge_to_broker", f"{amount_usd} USD")
        return record

    def bridge_from_broker(
        self, amount_usd: float, confirm: str | None = None
    ) -> TransferRecord:
        if amount_usd <= 0:
            raise WalletError("amount must be positive")
        if self.safety is not None:
            try:
                self.safety.assert_send_allowed()
            except Exception as exc:
                raise WalletError(str(exc)) from exc
        if self.cfg.require_send_confirmation:
            from pi_invest.wallet.confirm import (
                bridge_phrase,
                confirmation_phrase,
                confirmations_match,
            )

            expected_send = confirmation_phrase("USD", amount_usd)
            expected_bridge = bridge_phrase(amount_usd)
            if not (
                confirmations_match(expected_send, confirm)
                or confirmations_match(expected_bridge, confirm)
            ):
                raise WalletError(
                    f"confirmation required — type exactly: {expected_bridge}"
                )
        self.db.ensure_paper_account(0.0)
        cash, _, _ = self.db.load_paper_state()
        if amount_usd > cash + 1e-9:
            raise WalletError(f"insufficient brokerage cash ({cash})")
        self.db.set_paper_cash(cash - amount_usd)
        self.db.adjust_wallet_balance("USD", amount_usd)
        record = TransferRecord(
            transfer_id=str(uuid.uuid4()),
            direction=TransferDirection.RECEIVE,
            asset="USD",
            amount=amount_usd,
            counterparty="broker:paper",
            network="internal",
            status=TransferStatus.COMPLETED,
            memo="bridge brokerage cash → wallet",
            paper=True,
            tx_ref=f"bridge-in-{secrets.token_hex(6)}",
        )
        self.db.save_transfer(record)
        self.db.audit("wallet.bridge_from_broker", f"{amount_usd} USD")
        return record

    def allowlist(self) -> list[dict[str, str]]:
        return self.db.list_allowlist()

    def allowlist_add(self, destination: str, label: str = "") -> None:
        self.db.add_allowlist(destination, label=label)
        self.db.audit("allowlist.add", destination)

    def allowlist_remove(self, destination: str) -> None:
        if not self.db.remove_allowlist(destination):
            raise WalletError(f"not on allowlist: {destination}")
        self.db.audit("allowlist.remove", destination)


def build_wallet(
    cfg: AppConfig,
    env: EnvSettings,
    db: Database,
    safety: "SafetyGate | None" = None,
) -> WalletService:
    from pi_invest.safety import SafetyGate

    wcfg = cfg.wallet
    gate = safety or SafetyGate(db)
    db.ensure_allowlist(wcfg.allowlist_bootstrap)
    if wcfg.backend == "coinbase":
        backend: WalletBackend = CoinbaseWallet(env, wcfg, db)
    else:
        backend = PaperWallet(db, wcfg)
    return WalletService(backend, db, wcfg, env, safety=gate)

