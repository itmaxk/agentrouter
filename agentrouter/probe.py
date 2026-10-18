"""Talks to AgentRouter: model discovery and one-token liveness probes."""
import fnmatch
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import auth

OK, DOWN, LIMITED, AUTH_ERROR, WRONG_ENDPOINT = "ok", "down", "limited", "auth_error", "wrong_endpoint"
UP_STATUSES = {OK}
# auth_error says nothing about the model itself, so it never changes availability history
NEUTRAL_STATUSES = {AUTH_ERROR}


@dataclass
class ProbeResult:
    model: str
    endpoint: str
    status: str
    latency_ms: int
    http_code: int = 0
    error: str = ""

    @property
    def up(self) -> bool:
        return self.status in UP_STATUSES


def _request(cfg, method, path, body=None, timeout=None):
    """Returns (http_code, parsed_json_or_None, raw_text)."""
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {auth.api_key()}",
            "User-Agent": auth.user_agent(cfg),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or cfg["intervals"]["timeout"]) as resp:
            code, raw = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        code, raw = e.code, e.read().decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    return code, data, raw


def _error_message(data, raw) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return err
        if data.get("message"):
            return str(data["message"])
    return raw[:300]


def list_models(cfg) -> list[dict]:
    code, data, raw = _request(cfg, "GET", "/models", timeout=30)
    if code != 200 or not isinstance(data, dict):
        raise RuntimeError(f"/models HTTP {code}: {_error_message(data, raw)}")
    models = []
    for m in data.get("data") or []:
        mid = m.get("id")
        if not mid:
            continue
        if not any(fnmatch.fnmatch(mid, p) for p in cfg["include"]):
            continue
        if any(fnmatch.fnmatch(mid, p) for p in cfg["exclude"]):
            continue
        models.append({"id": mid, "endpoint_types": m.get("supported_endpoint_types") or []})
    return models


def _body(model, endpoint):
    if endpoint == "responses":
        return "/responses", {"model": model, "input": "ping", "max_output_tokens": 16}
    return "/chat/completions", {"model": model, "messages": [{"role": "user", "content": "ping"}],
                                 "max_tokens": 1, "stream": False}


def classify(endpoint, code, data, raw) -> tuple[str, str]:
    msg = _error_message(data, raw)
    if code == 200:
        if isinstance(data, dict) and not data.get("error"):
            if endpoint == "responses" and data.get("output") is not None:
                return OK, ""
            if endpoint == "chat" and data.get("choices"):
                return OK, ""
        return DOWN, f"bad 200 response: {msg}"
    if code == 400 and endpoint == "chat" and "/v1/responses" in msg:
        return WRONG_ENDPOINT, msg
    if code in (401, 403):
        return AUTH_ERROR, msg
    if code in (402, 429):  # quota exhausted / rate limited
        return LIMITED, msg
    return DOWN, msg


def probe(cfg, model: str, endpoint: str = "chat") -> ProbeResult:
    path, body = _body(model, endpoint)
    t0 = time.monotonic()
    try:
        code, data, raw = _request(cfg, "POST", path, body)
        status, err = classify(endpoint, code, data, raw)
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        code, status, err = 0, DOWN, f"{type(e).__name__}: {getattr(e, 'reason', e)}"
    latency = int((time.monotonic() - t0) * 1000)
    return ProbeResult(model, endpoint, status, latency, code, err[:500])


def probe_auto(cfg, model: str, endpoint: str = "chat") -> ProbeResult:
    """Probe on the known endpoint, switching to the Responses API when chat is refused."""
    res = probe(cfg, model, endpoint)
    if res.status == WRONG_ENDPOINT:
        res = probe(cfg, model, "responses")
    return res
