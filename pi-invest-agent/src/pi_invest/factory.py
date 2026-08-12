from __future__ import annotations

from pathlib import Path

from pi_invest.agent import InvestAgent
from pi_invest.alerts import AlertBus, build_alerts
from pi_invest.broker import build_broker
from pi_invest.config import AppConfig, EnvSettings, load_config, load_env
from pi_invest.data import build_market_data
from pi_invest.journal import PerformanceJournal
from pi_invest.safety import SafetyGate
from pi_invest.storage.db import Database
from pi_invest.wallet import WalletService, build_wallet


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_agent(
    config_path: str | None = None,
    force_simulator: bool = False,
) -> tuple[
    InvestAgent,
    AppConfig,
    EnvSettings,
    Database,
    WalletService,
    SafetyGate,
    PerformanceJournal,
    AlertBus,
]:
    root = project_root()
    env = load_env()

    cfg_path = Path(config_path or env.pi_invest_config)
    if not cfg_path.is_absolute():
        cfg_path = root / cfg_path
    if not cfg_path.exists():
        example = root / "config" / "config.example.yaml"
        cfg_path = example

    cfg = load_config(cfg_path)

    if env.pi_invest_local_only:
        # Self-contained Pi mode: local LLM + offline market + paper books
        cfg.llm.provider = "ollama"
        cfg.llm.timeout_seconds = max(cfg.llm.timeout_seconds, 120)
        cfg.alerts.enabled = False
        cfg.agent.mode = "paper"
        cfg.agent.allow_simulator_fallback = True
        cfg.broker.backend = "paper"
        cfg.wallet.backend = "paper"
        cfg.schedule.prefer_market_hours = False
        if not env.pi_invest_allow_online_quotes:
            cfg.market.provider = "simulator"

    db_path = Path(env.pi_invest_db)
    if not db_path.is_absolute():
        db_path = root / db_path
    db = Database(db_path)

    alerts = build_alerts(cfg.alerts, env, db)
    safety = SafetyGate(db, alerts=alerts)
    journal = PerformanceJournal(db, safety, alerts=alerts)

    provider = "simulator" if force_simulator else cfg.market.provider
    if env.pi_invest_local_only and provider not in {"simulator", "yahoo"}:
        provider = "simulator"
    market = build_market_data(
        provider=provider,
        allow_simulator_fallback=cfg.agent.allow_simulator_fallback,
    )
    broker = build_broker(cfg, env, db)
    wallet = build_wallet(cfg, env, db, safety=safety)
    agent = InvestAgent(
        cfg,
        env,
        market,
        broker,
        db,
        safety=safety,
        journal=journal,
        wallet=wallet,
        alerts=alerts,
    )
    return agent, cfg, env, db, wallet, safety, journal, alerts
