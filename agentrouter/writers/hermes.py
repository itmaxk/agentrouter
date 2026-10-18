"""Hermes: the first `fallback_providers` entry points at AgentRouter through a named `providers:` block.

Only the top-level `fallback_providers:` block is rewritten (and `providers.<name>` added once);
the rest of config.yaml is kept byte for byte. Hermes re-reads the fallback chain every turn.
"""
import re
import shutil
from pathlib import Path

from . import atomic_write, backup
from .. import auth

API_MODES = {"chat": "chat_completions", "responses": "codex_responses"}


def _yaml():
    try:
        import yaml
    except ImportError as e:
        raise RuntimeError("Hermes support needs PyYAML (pip install pyyaml)") from e
    return yaml


def _top_block(text: str, key: str) -> tuple[int, int] | None:
    """Span of a top-level `key:` block: its line plus following indented/list/blank lines."""
    m = re.search(rf"^{re.escape(key)}:.*(?:\n|$)", text, re.M)
    if not m:
        return None
    end = m.end()
    for line in re.finditer(r"^.*(?:\n|$)", text[end:], re.M):
        s = line.group()
        if not s:
            break
        if s.strip() == "" or s[0] in " \t-#":
            end += len(s)
        else:
            break
    # leave trailing blank lines to the next block
    while end > m.end() and text[:end].endswith("\n\n"):
        end -= 1
    return m.start(), end


def _agentrouter_cmd() -> str:
    return shutil.which("agentrouter") or str(Path("~/.local/bin/agentrouter").expanduser())


class HermesWriter:
    name = "hermes"

    def __init__(self, tcfg: dict, cfg: dict):
        self.path = Path(tcfg["path"])
        self.provider = tcfg["provider"]
        self.keep_existing = tcfg.get("keep_existing", True)
        self.cfg = cfg

    def _load(self, text):
        return _yaml().safe_load(text) or {}

    def current(self) -> str | None:
        if not self.path.exists():
            return None
        for e in self._load(self.path.read_text()).get("fallback_providers") or []:
            if isinstance(e, dict) and e.get("provider") in (self.provider, f"custom:{self.provider}"):
                return e.get("model")
        return None

    def managed(self) -> bool:
        return self.path.exists()

    def provider_block(self) -> dict:
        return {
            "name": "AgentRouter",
            "api": self.cfg["client_base_url"],
            "key_cmd": f"{_agentrouter_cmd()} key",
            "transport": "chat_completions",
            # AgentRouter only serves known coding clients; the OpenAI SDK default UA is refused
            "extra_headers": {"User-Agent": auth.user_agent(self.cfg)},
        }

    def render(self, text: str, model: str, endpoint: str = "chat") -> str:
        yaml = _yaml()
        data = self._load(text)
        entry = {"provider": self.provider, "model": model, "api_mode": API_MODES.get(endpoint, "chat_completions")}
        chain = [entry]
        if self.keep_existing:
            chain += [e for e in data.get("fallback_providers") or []
                      if not (isinstance(e, dict) and e.get("provider") in (self.provider, f"custom:{self.provider}"))]
        block = yaml.safe_dump({"fallback_providers": chain}, default_flow_style=False, sort_keys=False)
        block = re.sub(r"^- ", "  - ", block, flags=re.M)
        block = re.sub(r"^  (?=\w)", "    ", block, flags=re.M)

        span = _top_block(text, "fallback_providers")
        if span:
            new = text[:span[0]] + block + text[span[1]:]
        else:
            new = text + ("" if text.endswith("\n") else "\n") + block

        providers = data.get("providers")
        if not (isinstance(providers, dict) and self.provider in providers):
            pblock = yaml.safe_dump({self.provider: self.provider_block()}, default_flow_style=False, sort_keys=False)
            pblock = "".join("  " + ln for ln in pblock.splitlines(True))
            pspan = _top_block(new, "providers")
            if pspan and isinstance(providers, dict):
                head_end = new.index("\n", pspan[0]) + 1
                new = new[:head_end] + pblock + new[head_end:]
            elif pspan:  # `providers:` present but empty/null
                new = new[:pspan[0]] + "providers:\n" + pblock + new[pspan[1]:]
            else:
                new = new + ("" if new.endswith("\n") else "\n") + "providers:\n" + pblock

        parsed = self._load(new)  # must still parse, with our entry first
        assert parsed["fallback_providers"][0]["model"] == model
        return new

    def apply(self, model: str, endpoint: str = "chat", dry_run=False) -> bool:
        text = self.path.read_text()
        new = self.render(text, model, endpoint)
        if new == text:
            return False
        if not dry_run:
            backup(self.path)
            atomic_write(self.path, new)
        return True
