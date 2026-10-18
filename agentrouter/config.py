"""User config: ~/.config/agentrouter/config.json deep-merged over DEFAULTS."""
import copy
import json
import os
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("AGENTROUTER_CONFIG", "~/.config/agentrouter/config.json")).expanduser()
DATA_DIR = Path(os.environ.get("AGENTROUTER_DATA", "~/.local/share/agentrouter")).expanduser()

DEFAULTS = {
    "base_url": "https://agentrouter.org/v1",
    # AgentRouter rejects unknown clients; empty means "opencode/<installed opencode version>"
    "user_agent": "",
    # Endpoint that opencode/hermes talk to (the null-event filtering proxy)
    "client_base_url": "http://127.0.0.1:18777/v1",
    # Most preferred first; models not listed rank below all listed ones
    "priority": ["claude-opus-5-5", "gpt-6.1-sol", "deepseek-v4-flash", "gpt-6-astra"],
    "include": ["*"],
    "exclude": [],
    "intervals": {
        "current": 60,      # model selected by any target
        "candidate": 300,   # next best models
        "other": 1200,
        "confirm": 20,      # re-check after a status flip before trusting it
        "discover": 1800,   # GET /v1/models
        "timeout": 45,
    },
    "candidates": 2,
    "selector": {
        "lookahead_min": 30,         # how far ahead predicted outages trigger an early switch
        "proactive_threshold": 0.5,
        "margin": 0.15,              # a rival must score this much higher to replace a working model
        "cooldown": 300,
        "min_stable": 600,           # a recovered model must stay up this long before switching back
    },
    "predict": {"half_life_days": 14, "step_sec": 300, "history_days": 60},
    "targets": {
        "opencode": {
            "enabled": True,
            "path": "~/.config/opencode/opencode.jsonc",
            "provider": "agentrouter",
            "endpoints": ["chat"],   # @ai-sdk/openai-compatible only speaks chat completions
            "catalog": "~/.cache/opencode/models.json",
        },
        "hermes": {
            "enabled": True,
            "path": "~/.hermes/config.yaml",
            "provider": "agentrouter",
            "endpoints": ["chat", "responses"],
            "keep_existing": True,   # keep other fallback_providers entries after ours
        },
    },
    "web": {"host": "127.0.0.1", "port": 18778},
    "probe_retention_days": 30,
}


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(path: Path = CONFIG_PATH) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path.exists():
        _merge(cfg, json.loads(path.read_text()))
    for t in cfg["targets"].values():
        for k in ("path", "catalog"):
            if k in t:
                t[k] = str(Path(t[k]).expanduser())
    return cfg


def write_default(path: Path = CONFIG_PATH) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULTS, indent=2) + "\n")
    return True
