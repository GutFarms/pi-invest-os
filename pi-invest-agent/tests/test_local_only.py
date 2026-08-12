from pathlib import Path

from pi_invest.config import AppConfig, EnvSettings, load_config
from pi_invest.factory import build_agent


def test_local_only_forces_on_device_stack(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
agent:
  mode: live
  allow_simulator_fallback: false
broker:
  backend: alpaca
wallet:
  backend: coinbase
market:
  provider: yahoo
llm:
  provider: openai
alerts:
  enabled: true
schedule:
  prefer_market_hours: true
""",
        encoding="utf-8",
    )
    db_path = tmp_path / "t.db"
    monkeypatch.setenv("PI_INVEST_LOCAL_ONLY", "true")
    monkeypatch.setenv("PI_INVEST_CONFIG", str(cfg_path))
    monkeypatch.setenv("PI_INVEST_DB", str(db_path))
    monkeypatch.chdir(tmp_path)

    agent, cfg, env, *_ = build_agent(str(cfg_path), force_simulator=False)
    assert env.pi_invest_local_only is True
    assert cfg.llm.provider == "ollama"
    assert cfg.broker.backend == "paper"
    assert cfg.wallet.backend == "paper"
    assert cfg.agent.mode == "paper"
    assert cfg.alerts.enabled is False
    assert cfg.market.provider == "simulator"
    assert cfg.schedule.prefer_market_hours is False
    assert agent is not None


def test_os_config_is_local_by_default():
    os_cfg = Path(__file__).resolve().parents[2] / "pi-invest-os" / "config" / "config.os.yaml"
    if not os_cfg.exists():
        # Agent-only checkout
        return
    cfg = load_config(os_cfg)
    assert isinstance(cfg, AppConfig)
    assert cfg.llm.provider == "ollama"
    assert cfg.market.provider == "simulator"
    assert cfg.broker.backend == "paper"
    assert cfg.wallet.backend == "paper"
    assert cfg.alerts.enabled is False
