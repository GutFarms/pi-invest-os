from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from pi_invest.config import EnvSettings, WalletConfig
from pi_invest.storage.db import Database
from pi_invest.wallet import CoinbaseWallet, WalletError
from pi_invest.wallet.coinbase_client import (
    CoinbaseAPIError,
    CoinbaseAccount,
    CoinbaseAddress,
    CoinbaseClient,
    build_cdp_jwt,
)


def _ec_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def test_build_cdp_jwt_roundtrip():
    pem = _ec_pem()
    token = build_cdp_jwt(
        "organizations/org/apiKeys/key",
        pem,
        "GET",
        "/api/v3/brokerage/accounts",
    )
    assert isinstance(token, str)
    assert token.count(".") == 2


def test_build_cdp_jwt_rejects_bad_secret():
    with pytest.raises(CoinbaseAPIError, match="EC private key"):
        build_cdp_jwt("organizations/org/apiKeys/key", "not-a-pem", "GET", "/v2/accounts")


def test_coinbase_client_list_accounts_mocked():
    env = EnvSettings(
        coinbase_api_key="organizations/o/apiKeys/k",
        coinbase_api_secret=_ec_pem(),
    )
    client = CoinbaseClient(env)
    payload = {
        "data": [
            {
                "id": "acc-usd",
                "name": "USD Wallet",
                "primary": True,
                "type": "fiat",
                "balance": {"amount": "125.50", "currency": "USD"},
            },
            {
                "id": "acc-btc",
                "name": "BTC Wallet",
                "primary": True,
                "type": "wallet",
                "balance": {"amount": "0.01", "currency": "BTC"},
            },
        ],
        "pagination": {},
    }
    with patch.object(client, "_request", return_value=payload) as req:
        accounts = client.list_app_accounts()
        req.assert_called()
    assert len(accounts) == 2
    assert accounts[0].currency == "USD"
    assert abs(accounts[0].balance - 125.50) < 1e-9
    with patch.object(client, "list_app_accounts", return_value=accounts):
        btc = client.account_for_currency("BTC")
    assert btc is not None
    assert btc.id == "acc-btc"


def test_coinbase_wallet_snapshot_and_send_gate(tmp_path):
    db = Database(tmp_path / "c.db")
    env = EnvSettings(
        coinbase_api_key="organizations/o/apiKeys/k",
        coinbase_api_secret=_ec_pem(),
        allow_live_transfers=False,
    )
    cfg = WalletConfig(backend="coinbase")
    wallet = CoinbaseWallet(env, cfg, db)

    accounts = [
        CoinbaseAccount(
            id="1", currency="USD", name="USD", balance=200.0, available=200.0, primary=True
        ),
        CoinbaseAccount(
            id="2", currency="BTC", name="BTC", balance=0.5, available=0.5, primary=True
        ),
        CoinbaseAccount(
            id="3", currency="ETH", name="ETH", balance=0.0, available=0.0, primary=True
        ),
        CoinbaseAccount(
            id="4", currency="USDC", name="USDC", balance=10.0, available=10.0, primary=True
        ),
    ]
    with patch.object(wallet.client, "list_app_accounts", return_value=accounts):
        snap = wallet.snapshot()
    assert snap.backend == "coinbase"
    usd = next(b for b in snap.balances if b.asset == "USD")
    assert abs(usd.amount - 200.0) < 1e-9

    with pytest.raises(WalletError, match="ALLOW_LIVE_TRANSFERS"):
        wallet.send("USD", 5.0, "friend@example.com")

    env2 = EnvSettings(
        coinbase_api_key=env.coinbase_api_key,
        coinbase_api_secret=env.coinbase_api_secret,
        allow_live_transfers=True,
    )
    # Bypass service-level gates by calling backend directly for this unit
    wallet2 = CoinbaseWallet(env2, cfg, db)
    with patch.object(
        wallet2.client,
        "send_money",
        return_value={"id": "tx-1", "status": "pending"},
    ) as send:
        record = wallet2.send("BTC", 0.001, "bc1qexample")
        send.assert_called_once()
    assert record.paper is False
    assert record.tx_ref == "tx-1"


def test_coinbase_receive_address_persisted(tmp_path):
    db = Database(tmp_path / "c.db")
    env = EnvSettings(
        coinbase_api_key="organizations/o/apiKeys/k",
        coinbase_api_secret=_ec_pem(),
    )
    wallet = CoinbaseWallet(env, WalletConfig(), db)
    fake = CoinbaseAddress(
        id="a1", address="bc1qliveaddress", network="bitcoin", currency="BTC"
    )
    with patch.object(wallet.client, "get_or_create_receive_address", return_value=fake):
        addr = wallet.receive_address("BTC")
    assert addr.address == "bc1qliveaddress"
    # Second call should still return upserted value from API path
    with patch.object(wallet.client, "get_or_create_receive_address", return_value=fake):
        addr2 = wallet.receive_address("BTC")
    assert addr2.address == "bc1qliveaddress"
