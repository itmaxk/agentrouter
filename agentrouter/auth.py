"""AgentRouter API key and client identity."""
import functools
import json
import os
import shutil
import subprocess
from pathlib import Path

OPENCODE_AUTH = Path("~/.local/share/opencode/auth.json").expanduser()


def api_key() -> str:
    key = os.environ.get("AGENTROUTER_API_KEY", "").strip()
    if key:
        return key
    try:
        key = json.loads(OPENCODE_AUTH.read_text())["agentrouter"]["key"].strip()
    except (OSError, KeyError, ValueError) as e:
        raise RuntimeError(f"no AgentRouter key: set AGENTROUTER_API_KEY or log in with opencode ({e})")
    return key


@functools.lru_cache(maxsize=None)
def opencode_version() -> str:
    exe = shutil.which("opencode") or str(Path("~/.opencode/bin/opencode").expanduser())
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        return out.split()[-1] if out else "1.0.0"
    except (OSError, subprocess.SubprocessError):
        return "1.0.0"


def user_agent(cfg: dict) -> str:
    return cfg.get("user_agent") or f"opencode/{opencode_version()}"
