from __future__ import annotations


def format_send_amount(asset: str, amount: float) -> str:
    if asset.upper() in {"USD", "USDC", "USDT"}:
        return f"{amount:.2f}"
    text = f"{amount:.8f}".rstrip("0").rstrip(".")
    return text or "0"


def confirmation_phrase(asset: str, amount: float) -> str:
    """Exact phrase the operator must type to authorize an outbound send."""
    return f"SEND {format_send_amount(asset, amount)} {asset.upper()}"


def bridge_phrase(amount: float) -> str:
    return f"BRIDGE {format_send_amount('USD', amount)} USD"


def confirmations_match(expected: str, provided: str | None) -> bool:
    if provided is None:
        return False
    return expected.strip() == provided.strip()
