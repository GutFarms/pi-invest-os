from __future__ import annotations

import secrets
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from pi_invest.agent import InvestAgent
from pi_invest.config import AppConfig, EnvSettings
from pi_invest.journal import PerformanceJournal
from pi_invest.safety import SafetyGate
from pi_invest.storage.db import Database
from pi_invest.wallet import WalletError, WalletService
from pi_invest.wallet.confirm import bridge_phrase, confirmation_phrase

security = HTTPBasic(auto_error=False)

_DASHBOARD_PATH = Path(__file__).with_name("dashboard.html")


def _load_dashboard_html() -> str:
    return _DASHBOARD_PATH.read_text(encoding="utf-8")


class SendBody(BaseModel):
    asset: str
    amount: float = Field(gt=0)
    to_address: str
    memo: str = ""
    confirm: str = ""


class ReceiveBody(BaseModel):
    asset: str
    amount: float = Field(gt=0)
    from_address: str = "external"
    memo: str = ""


class HaltBody(BaseModel):
    reason: str = "dashboard halt"


class AllowAddBody(BaseModel):
    destination: str
    label: str = ""


class AllowRemoveBody(BaseModel):
    destination: str


class DashboardUser(BaseModel):
    username: str
    role: Literal["admin", "viewer"] = "admin"


def create_app(
    agent: InvestAgent,
    cfg: AppConfig,
    db: Database,
    wallet: WalletService,
    env: EnvSettings | None = None,
    safety: SafetyGate | None = None,
    journal: PerformanceJournal | None = None,
) -> FastAPI:
    env = env or EnvSettings()
    safety = safety or SafetyGate(db)
    journal = journal or PerformanceJournal(db, safety)
    app = FastAPI(title="Pi Invest Agent", version="0.1.0")

    def _match(username: str, password: str, expect_user: str, expect_pass: str) -> bool:
        if not expect_pass:
            return False
        user_ok = secrets.compare_digest(
            username.encode(), expect_user.encode()
        )
        pass_ok = secrets.compare_digest(
            password.encode(), expect_pass.encode()
        )
        return user_ok and pass_ok

    def require_user(
        request: Request,
        credentials: HTTPBasicCredentials | None = Depends(security),
    ) -> DashboardUser:
        """Return admin or viewer identity."""
        if not cfg.dashboard.require_auth or not env.dashboard_password:
            return DashboardUser(
                username=env.dashboard_username or "anonymous",
                role="admin",
            )
        # Local desktop app on the Pi — no browser login dialog
        client_host = request.client.host if request.client else ""
        if env.pi_invest_desktop_trust_loopback and client_host in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            return DashboardUser(
                username=env.dashboard_username or "local",
                role="admin",
            )
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Basic"},
            )
        if _match(
            credentials.username,
            credentials.password,
            env.dashboard_username,
            env.dashboard_password,
        ):
            return DashboardUser(username=credentials.username, role="admin")
        if _match(
            credentials.username,
            credentials.password,
            env.dashboard_readonly_username,
            env.dashboard_readonly_password,
        ):
            return DashboardUser(username=credentials.username, role="viewer")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    def require_admin(
        user: DashboardUser = Depends(require_user),
    ) -> DashboardUser:
        if user.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin credentials required for this action",
            )
        return user

    @app.get("/", response_class=HTMLResponse)
    def home(_user: DashboardUser = Depends(require_user)) -> str:
        return _load_dashboard_html()

    @app.get("/api/status")
    def status_api(user: DashboardUser = Depends(require_user)) -> dict:
        marks = {}
        for sym in cfg.universe:
            try:
                marks[sym] = agent.market.get_quote(sym).price
            except Exception:  # noqa: BLE001
                pass
        account = agent.broker.get_account(marks)
        snap = wallet.snapshot()
        # Ensure receive addresses present for QR (paper or coinbase)
        addresses = dict(snap.addresses)
        for asset in cfg.wallet.assets:
            if asset.upper() == "USD":
                continue
            if asset.upper() in addresses and addresses[asset.upper()].address:
                continue
            try:
                addresses[asset.upper()] = wallet.receive_info(asset)
            except Exception:  # noqa: BLE001
                pass
        snap.addresses = addresses
        summary = journal.summary()
        halt = safety.state()
        ollama_status: dict = {"ok": False, "detail": "not checked"}
        if cfg.llm.provider == "ollama":
            try:
                import httpx

                base = env.ollama_base_url.rstrip("/")
                with httpx.Client(timeout=2.0) as client:
                    resp = client.get(f"{base}/api/tags")
                    resp.raise_for_status()
                    models = [
                        m.get("name")
                        for m in (resp.json().get("models") or [])
                        if isinstance(m, dict)
                    ]
                wanted = env.ollama_model
                ollama_status = {
                    "ok": True,
                    "detail": "up",
                    "models": models,
                    "has_configured_model": any(
                        m == wanted or m.startswith(f"{wanted}:") or wanted.startswith(f"{m}:")
                        for m in models
                    ),
                }
            except Exception as exc:  # noqa: BLE001
                ollama_status = {"ok": False, "detail": str(exc)}
        return {
            "mode": cfg.agent.mode,
            "backend": cfg.broker.backend,
            "agent": cfg.agent.name,
            "local_only": bool(env.pi_invest_local_only),
            "llm": {
                "provider": cfg.llm.provider,
                "model": env.ollama_model
                if cfg.llm.provider == "ollama"
                else env.openai_model,
                "base_url": env.ollama_base_url
                if cfg.llm.provider == "ollama"
                else None,
            },
            "ollama": ollama_status,
            "market_provider": cfg.market.provider,
            "role": user.role,
            "username": user.username,
            "halted": halt.halted,
            "halt_reason": halt.reason,
            "account": account.model_dump(),
            "wallet": snap.model_dump(mode="json"),
            "journal": summary.model_dump(mode="json"),
            "journal_rows": [r.model_dump(mode="json") for r in journal.history(60)],
            "allowlist": wallet.allowlist(),
            "audit": db.recent_audit(20),
            "confirm_example": confirmation_phrase("USD", 25.0),
            "require_send_confirmation": cfg.wallet.require_send_confirmation,
            "allowlist_required": cfg.wallet.allowlist_required,
            "transfers": [t.model_dump(mode="json") for t in wallet.history(15)],
            "orders": db.recent_orders(15),
        }

    @app.post("/api/cycle")
    def cycle(_user: DashboardUser = Depends(require_admin)) -> dict:
        decision = agent.run_cycle(dry_run=False)
        db.audit("invest.cycle", decision.cycle_id)
        return decision.model_dump(mode="json")

    @app.post("/api/halt")
    def halt_api(
        body: HaltBody, _user: DashboardUser = Depends(require_admin)
    ) -> dict:
        st = safety.halt(body.reason)
        db.audit("safety.halt", body.reason)
        return st.model_dump(mode="json")

    @app.post("/api/resume")
    def resume_api(_user: DashboardUser = Depends(require_admin)) -> dict:
        st = safety.resume()
        db.audit("safety.resume", "ok")
        return st.model_dump(mode="json")

    @app.get("/api/journal")
    def journal_api(_user: DashboardUser = Depends(require_user)) -> dict:
        return {
            "summary": journal.summary().model_dump(mode="json"),
            "rows": [r.model_dump(mode="json") for r in journal.history(100)],
        }

    @app.post("/api/wallet/send")
    def wallet_send(
        body: SendBody, _user: DashboardUser = Depends(require_admin)
    ) -> dict:
        try:
            return wallet.send(
                body.asset,
                body.amount,
                body.to_address,
                memo=body.memo,
                confirm=body.confirm,
            ).model_dump(mode="json")
        except WalletError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/wallet/receive")
    def wallet_receive(
        body: ReceiveBody, _user: DashboardUser = Depends(require_admin)
    ) -> dict:
        try:
            return wallet.receive(
                body.asset,
                body.amount,
                from_address=body.from_address,
                memo=body.memo,
            ).model_dump(mode="json")
        except WalletError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/wallet/confirm-phrase")
    def confirm_phrase_api(
        asset: str,
        amount: float,
        _user: DashboardUser = Depends(require_user),
    ) -> dict:
        return {
            "phrase": confirmation_phrase(asset, amount),
            "bridge_phrase": bridge_phrase(amount) if asset.upper() == "USD" else None,
        }

    @app.post("/api/wallet/allowlist/add")
    def allow_add(
        body: AllowAddBody, _user: DashboardUser = Depends(require_admin)
    ) -> dict:
        try:
            wallet.allowlist_add(body.destination, label=body.label)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "allowlist": wallet.allowlist()}

    @app.post("/api/wallet/allowlist/remove")
    def allow_remove(
        body: AllowRemoveBody, _user: DashboardUser = Depends(require_admin)
    ) -> dict:
        try:
            wallet.allowlist_remove(body.destination)
        except WalletError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "allowlist": wallet.allowlist()}

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "agent": cfg.agent.name, "halted": safety.is_halted()}

    return app
