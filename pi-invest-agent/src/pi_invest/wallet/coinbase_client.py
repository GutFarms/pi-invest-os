from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx

from pi_invest.config import EnvSettings


class CoinbaseAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass
class CoinbaseAccount:
    id: str
    currency: str
    name: str
    balance: float
    available: float
    hold: float = 0.0
    type: str = ""
    primary: bool = False


@dataclass
class CoinbaseAddress:
    id: str
    address: str
    network: str
    currency: str
    name: str = ""


def _normalize_pem(secret: str) -> str:
    """Allow .env-style single-line PEMs with escaped newlines."""
    s = secret.strip().strip('"').strip("'")
    if "\\n" in s and "BEGIN" in s:
        s = s.replace("\\n", "\n")
    return s


def build_cdp_jwt(
    api_key: str,
    api_secret: str,
    method: str,
    path: str,
    host: str = "api.coinbase.com",
) -> str:
    """
    Build a Coinbase Developer Platform (CDP) JWT for Coinbase App / Advanced Trade.

    api_key should look like: organizations/{org_id}/apiKeys/{key_id}
    api_secret should be an EC PEM private key (ECDSA / ES256).
    """
    try:
        import jwt
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:  # pragma: no cover
        raise CoinbaseAPIError(
            "PyJWT and cryptography are required for Coinbase auth. "
            "Reinstall with: pip install 'pi-invest-agent[coinbase]' "
            "or pip install PyJWT cryptography"
        ) from exc

    pem = _normalize_pem(api_secret)
    try:
        private_key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except Exception as exc:
        raise CoinbaseAPIError(
            "COINBASE_API_SECRET must be an EC private key PEM from the CDP portal "
            "(ECDSA). Create a Coinbase App API key and select ECDSA, not Ed25519."
        ) from exc

    # Path for JWT uri claim must exclude query string
    path_only = path.split("?", 1)[0]
    uri = f"{method.upper()} {host}{path_only}"
    now = int(time.time())
    payload = {
        "sub": api_key,
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
        "uri": uri,
    }
    return jwt.encode(
        payload,
        private_key,
        algorithm="ES256",
        headers={"kid": api_key, "nonce": secrets.token_hex(16)},
    )


class CoinbaseClient:
    """Thin Coinbase App REST client (CDP JWT auth)."""

    HOST = "api.coinbase.com"
    BASE = f"https://{HOST}"

    def __init__(self, env: EnvSettings, timeout: float = 30.0) -> None:
        if not env.coinbase_api_key or not env.coinbase_api_secret:
            raise CoinbaseAPIError(
                "Set COINBASE_API_KEY and COINBASE_API_SECRET "
                "(CDP key name + ECDSA PEM secret)"
            )
        self.api_key = env.coinbase_api_key.strip()
        self.api_secret = env.coinbase_api_secret
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        # Build JWT against path without query; httpx adds params separately
        jwt_token = build_cdp_jwt(
            self.api_key, self.api_secret, method, path, host=self.HOST
        )
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "pi-invest-agent/0.1",
        }
        url = f"{self.BASE}{path}"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.request(
                method.upper(), url, headers=headers, json=json_body, params=params
            )
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = resp.text
            raise CoinbaseAPIError(
                f"Coinbase {method.upper()} {path} failed ({resp.status_code}): {body}",
                status_code=resp.status_code,
                body=body,
            )
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    def ping(self) -> dict[str, Any]:
        """Connectivity check via Advanced Trade accounts (view permission)."""
        data = self._request("GET", "/api/v3/brokerage/accounts", params={"limit": 1})
        accounts = data.get("accounts") or []
        return {
            "ok": True,
            "advanced_trade_accounts": len(accounts),
            "has_next": bool(data.get("has_next")),
        }

    def list_app_accounts(self) -> list[CoinbaseAccount]:
        """List Coinbase App wallets (USD + crypto balances)."""
        out: list[CoinbaseAccount] = []
        path = "/v2/accounts"
        params: dict[str, Any] = {"limit": 100}
        while True:
            data = self._request("GET", path, params=params)
            for item in data.get("data") or []:
                bal = item.get("balance") or {}
                currency = str(bal.get("currency") or item.get("currency") or "").upper()
                amount = float(bal.get("amount") or 0)
                # available may be in native_balance or separate fields depending on API
                available = amount
                if item.get("available_balance"):
                    available = float(
                        (item["available_balance"] or {}).get("amount") or amount
                    )
                out.append(
                    CoinbaseAccount(
                        id=str(item.get("id")),
                        currency=currency,
                        name=str(item.get("name") or currency),
                        balance=amount,
                        available=available,
                        type=str(item.get("type") or ""),
                        primary=bool(item.get("primary")),
                    )
                )
            pagination = data.get("pagination") or {}
            next_uri = pagination.get("next_uri")
            if not next_uri:
                break
            # next_uri is like /v2/accounts?starting_after=...
            if next_uri.startswith("http"):
                # unexpected absolute — stop
                break
            path = next_uri.split("?", 1)[0]
            params = {}
            if "?" in next_uri:
                from urllib.parse import parse_qs, urlparse

                q = parse_qs(urlparse(next_uri).query)
                params = {k: v[0] for k, v in q.items()}
        return out

    def account_for_currency(self, currency: str) -> CoinbaseAccount | None:
        currency = currency.upper()
        accounts = self.list_app_accounts()
        # Prefer primary wallet for that currency
        matches = [a for a in accounts if a.currency == currency]
        if not matches:
            return None
        for a in matches:
            if a.primary:
                return a
        return matches[0]

    def list_addresses(self, account_id: str) -> list[CoinbaseAddress]:
        data = self._request("GET", f"/v2/accounts/{account_id}/addresses")
        out: list[CoinbaseAddress] = []
        for item in data.get("data") or []:
            out.append(
                CoinbaseAddress(
                    id=str(item.get("id")),
                    address=str(item.get("address") or ""),
                    network=str(item.get("network") or item.get("type") or ""),
                    currency=str(
                        (item.get("currency") or item.get("network") or "")
                    ).upper(),
                    name=str(item.get("name") or ""),
                )
            )
        return out

    def create_address(self, account_id: str, name: str = "pi-invest") -> CoinbaseAddress:
        data = self._request(
            "POST",
            f"/v2/accounts/{account_id}/addresses",
            json_body={"name": name},
        )
        item = data.get("data") or data
        return CoinbaseAddress(
            id=str(item.get("id")),
            address=str(item.get("address") or ""),
            network=str(item.get("network") or item.get("type") or ""),
            currency=str(item.get("currency") or "").upper(),
            name=str(item.get("name") or name),
        )

    def get_or_create_receive_address(
        self, currency: str, name: str = "pi-invest"
    ) -> CoinbaseAddress:
        acct = self.account_for_currency(currency)
        if acct is None:
            raise CoinbaseAPIError(f"No Coinbase account found for {currency}")
        existing = self.list_addresses(acct.id)
        if existing and existing[0].address:
            addr = existing[0]
            if not addr.currency:
                addr.currency = currency.upper()
            return addr
        created = self.create_address(acct.id, name=name)
        if not created.currency:
            created.currency = currency.upper()
        return created

    def send_money(
        self,
        currency: str,
        amount: float,
        to: str,
        *,
        description: str = "",
        network: str | None = None,
        destination_tag: str | None = None,
        idem: str | None = None,
    ) -> dict[str, Any]:
        import uuid as _uuid

        acct = self.account_for_currency(currency)
        if acct is None:
            raise CoinbaseAPIError(f"No Coinbase account found for {currency}")
        if currency.upper() in {"USD", "USDC", "USDT"}:
            amount_str = f"{amount:.2f}"
        else:
            amount_str = f"{amount:.8f}".rstrip("0").rstrip(".")
        body: dict[str, Any] = {
            "type": "send",
            "to": to,
            "amount": amount_str,
            "currency": currency.upper(),
            "idem": (idem or str(_uuid.uuid4())).lower(),
        }
        if description:
            body["description"] = description
        if network:
            body["network"] = network
        if destination_tag:
            body["destination_tag"] = destination_tag
        data = self._request(
            "POST", f"/v2/accounts/{acct.id}/transactions", json_body=body
        )
        return data.get("data") or data

    def list_transactions(self, currency: str, limit: int = 25) -> list[dict[str, Any]]:
        acct = self.account_for_currency(currency)
        if acct is None:
            return []
        data = self._request(
            "GET",
            f"/v2/accounts/{acct.id}/transactions",
            params={"limit": limit},
        )
        return list(data.get("data") or [])
