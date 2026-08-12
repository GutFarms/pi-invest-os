from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RiskConfig(BaseModel):
    max_position_pct: float = 0.15
    max_daily_loss_pct: float = 0.03
    cash_reserve_pct: float = 0.10
    max_open_positions: int = 8
    min_confidence: float = 0.55
    min_trade_notional: float = 25.0


class GuardConfig(BaseModel):
    """Alinia-style investment guard — validate AI intents against scores."""

    enabled: bool = True
    # LLM buys must also clear heuristic buy set unless proxy is very strong
    require_score_agreement: bool = True
    agreement_bypass_proxy: float = 0.62
    # Quality floor for new buys (profitability via selectivity)
    min_buy_income_proxy: float = 0.48
    # Refuse LLM sells of names still scoring as healthy income
    max_sell_income_proxy: float = 0.42
    # Cap LLM-reported confidence vs quantitative composite
    max_confidence_above_composite: float = 0.12
    max_llm_target_weight: float = 0.15
    prefer_income_etfs: bool = True
    income_etf_boost: float = 0.02
    # Concentrate capital in best ideas
    max_buy_intents: int = 4
    # Drawdown protection (fraction, e.g. 0.04 = 4%)
    drawdown_throttle_pct: float = 0.04
    drawdown_halt_buys_pct: float = 0.08
    audit_intents: bool = True


class BrokerConfig(BaseModel):
    backend: Literal["paper", "alpaca"] = "paper"
    starting_cash: float = 10_000.0


class MarketConfig(BaseModel):
    lookback_days: int = 60
    provider: Literal["yahoo", "simulator"] = "yahoo"


class LlmConfig(BaseModel):
    provider: Literal["none", "ollama", "openai"] = "none"
    temperature: float = 0.2
    timeout_seconds: int = 45


class ScheduleConfig(BaseModel):
    interval_minutes: int = 60
    market_tz: str = "America/New_York"
    prefer_market_hours: bool = True


class DashboardConfig(BaseModel):
    # Default localhost — use Tailscale/SSH tunnel or set 0.0.0.0 deliberately
    host: str = "127.0.0.1"
    port: int = 8787
    require_auth: bool = True


class AlertsConfig(BaseModel):
    enabled: bool = True
    # Topic comes from NTFY_TOPIC in .env; server defaults to ntfy.sh
    drawdown_alert_pct: float = 0.05  # notify when drawdown reaches 5%


class WalletConfig(BaseModel):
    """Fiat + crypto treasury for send/receive (separate from brokerage book)."""

    backend: Literal["paper", "coinbase"] = "paper"
    assets: list[str] = Field(
        default_factory=lambda: ["USD", "USDC", "BTC", "ETH"]
    )
    starting_balances: dict[str, float] = Field(
        default_factory=lambda: {
            "USD": 1000.0,
            "USDC": 0.0,
            "BTC": 0.0,
            "ETH": 0.0,
        }
    )
    networks: dict[str, str] = Field(
        default_factory=lambda: {
            "USD": "ach-sim",
            "USDC": "ethereum",
            "BTC": "bitcoin",
            "ETH": "ethereum",
        }
    )
    min_send: dict[str, float] = Field(
        default_factory=lambda: {
            "USD": 1.0,
            "USDC": 1.0,
            "BTC": 0.0001,
            "ETH": 0.001,
        }
    )
    send_fee: dict[str, float] = Field(
        default_factory=lambda: {
            "USD": 0.0,
            "USDC": 0.0,
            "BTC": 0.00001,
            "ETH": 0.0002,
        }
    )
    # Hard cap on outbound wallet sends (USD-equivalent) per UTC day
    max_daily_send_usd: float = 250.0
    # When true, external sends only go to DB allowlist destinations
    allowlist_required: bool = True
    # Seeded into the DB allowlist on first run (emails, addresses, account ids)
    allowlist_bootstrap: list[str] = Field(default_factory=list)
    # Require typing "SEND <amount> <ASSET>" (or --yes with matching --confirm)
    require_send_confirmation: bool = True


class SafetyConfig(BaseModel):
    """Kill-switch defaults — halt blocks trading and outbound sends."""

    journal_enabled: bool = True
    # Append security-sensitive actions to audit log
    audit_enabled: bool = True



class AgentConfig(BaseModel):
    name: str = "pi-income-agent"
    mode: Literal["paper", "live"] = "paper"
    allow_simulator_fallback: bool = True


class AppConfig(BaseModel):
    agent: AgentConfig = Field(default_factory=AgentConfig)
    universe: list[str] = Field(
        default_factory=lambda: ["SCHD", "VYM", "JEPI", "QQQ", "SPY", "BND"]
    )
    risk: RiskConfig = Field(default_factory=RiskConfig)
    guard: GuardConfig = Field(default_factory=GuardConfig)
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    market: MarketConfig = Field(default_factory=MarketConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    wallet: WalletConfig = Field(default_factory=WalletConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.2:3b"
    allow_live_trading: bool = False
    allow_live_transfers: bool = False
    coinbase_api_key: str = ""
    coinbase_api_secret: str = ""
    dashboard_username: str = "pi"
    dashboard_password: str = ""
    # Optional read-only dashboard user (cannot send/halt/cycle/edit allowlist)
    dashboard_readonly_username: str = "viewer"
    dashboard_readonly_password: str = ""
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    ntfy_token: str = ""
    pi_invest_config: str = "config/config.yaml"
    pi_invest_db: str = "data/pi_invest.db"
    # When true: force on-device Ollama, paper stack defaults, no cloud LLM/alerts
    pi_invest_local_only: bool = False
    # When true with local_only: keep yahoo quotes (still no cloud LLM)
    pi_invest_allow_online_quotes: bool = False
    # Desktop app / loopback clients skip HTTP basic auth (local Pi display)
    pi_invest_desktop_trust_loopback: bool = True


def load_config(path: str | Path | None = None) -> AppConfig:
    env = EnvSettings()
    cfg_path = Path(path or env.pi_invest_config)
    if not cfg_path.exists():
        example = cfg_path.parent / "config.example.yaml"
        if example.exists():
            cfg_path = example
        else:
            return AppConfig()
    with cfg_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return AppConfig.model_validate(raw)


def load_env() -> EnvSettings:
    return EnvSettings()
