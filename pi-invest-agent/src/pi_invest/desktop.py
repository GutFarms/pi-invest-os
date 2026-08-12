"""Native desktop window for Pi Invest (no browser URL bar)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

from pi_invest.config import AppConfig, EnvSettings
from pi_invest.factory import build_agent


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def _healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=2) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


def _start_uvicorn_thread(
    host: str,
    port: int,
    config_path: str | None,
) -> None:
    import uvicorn

    from pi_invest.web.app import create_app

    agent, cfg, env, db, wallet, safety, journal, _alerts = build_agent(
        config_path=config_path
    )
    # Desktop window is local — skip HTTP basic auth popup
    cfg.dashboard.require_auth = False
    api = create_app(agent, cfg, db, wallet, env=env, safety=safety, journal=journal)

    def _run() -> None:
        uvicorn.run(api, host=host, port=port, log_level="warning")

    t = threading.Thread(target=_run, name="pi-invest-uvicorn", daemon=True)
    t.start()


def _ensure_backend(host: str, port: int, config_path: str | None) -> str:
    base = f"http://{host}:{port}"
    if _healthy(base):
        return base

    # Prefer existing systemd unit when present
    try:
        subprocess.run(
            ["systemctl", "start", "pi-invest-dashboard.service"],
            check=False,
            capture_output=True,
            timeout=20,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        pass

    for _ in range(20):
        if _healthy(base):
            return base
        time.sleep(0.5)

    if not _port_open(host, port):
        _start_uvicorn_thread(host, port, config_path)
        for _ in range(40):
            if _healthy(base):
                return base
            time.sleep(0.25)

    if not _healthy(base):
        raise RuntimeError(
            f"Pi Invest backend is not ready at {base}. "
            "Try: sudo systemctl start pi-invest-dashboard"
        )
    return base


def _open_webview(url: str, title: str = "Pi Invest") -> bool:
    try:
        import webview  # type: ignore
    except ImportError:
        return False

    webview.create_window(
        title,
        url,
        width=1280,
        height=840,
        min_size=(900, 600),
        background_color="#07110d",
        text_select=True,
    )
    # gtk is preferred on Raspberry Pi OS; pywebview picks an available GUI
    webview.start(gui=os.environ.get("PI_INVEST_WEBVIEW_GUI") or None)
    return True


def _open_chromium_app(url: str) -> bool:
    for binary in ("chromium", "chromium-browser"):
        path = _which(binary)
        if not path:
            continue
        # Dedicated profile + app mode = no URL chrome, feels like a local app
        profile = os.path.expanduser("~/.local/share/pi-invest-app-profile")
        os.makedirs(profile, exist_ok=True)
        cmd = [
            path,
            f"--user-data-dir={profile}",
            "--class=PiInvest",
            f"--app={url}",
            "--no-first-run",
            "--disable-session-crashed-bubble",
            "--check-for-update-interval=31536000",
            "--disable-features=TranslateUI",
        ]
        os.execv(path, cmd)
    return False


def _which(name: str) -> str | None:
    from shutil import which

    return which(name)


def run_desktop_app(
    config_path: str | None = None,
    host: str | None = None,
    port: int | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    """Launch Pi Invest as a native desktop window on the Pi."""
    _log = log or (lambda m: print(m, file=sys.stderr))
    env = EnvSettings()
    cfg = AppConfig()
    try:
        agent, cfg, env, *_rest = build_agent(config_path=config_path)
        del agent, _rest
    except Exception:  # noqa: BLE001
        pass

    host = host or cfg.dashboard.host or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = int(port or cfg.dashboard.port or 8787)

    _log("Starting Pi Invest app…")
    url = _ensure_backend(host, port, config_path)
    _log("Opening local app window (not a browser tab).")

    if _open_webview(url):
        return

    _log("pywebview not available — using Chromium app window.")
    if _open_chromium_app(url):
        return

    raise RuntimeError(
        "No desktop UI backend found. Install: pip install pywebview "
        "and apt packages gir1.2-webkit2-4.1 (or chromium)."
    )
