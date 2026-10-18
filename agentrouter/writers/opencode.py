"""opencode: top-level `"model": "<provider>/<model>"` in opencode.jsonc."""
import json
from pathlib import Path

from . import atomic_write, backup, jsonc


class OpencodeWriter:
    name = "opencode"

    def __init__(self, tcfg: dict):
        self.path = Path(tcfg["path"])
        self.provider = tcfg["provider"]
        self.catalog = Path(tcfg["catalog"]) if tcfg.get("catalog") else None

    def current(self) -> str | None:
        """Model id if opencode currently uses our provider, else None (we only manage our provider)."""
        if not self.path.exists():
            return None
        model = jsonc.loads(self.path.read_text()).get("model") or ""
        prefix = self.provider + "/"
        return model[len(prefix):] if model.startswith(prefix) else None

    def managed(self) -> bool:
        return self.current() is not None

    def _catalog_models(self) -> set:
        try:
            return set(json.loads(self.catalog.read_text())[self.provider]["models"])
        except (OSError, KeyError, ValueError, TypeError, AttributeError):
            return set()

    def render(self, text: str, model: str) -> str:
        root = jsonc.parse(text)
        full = json.dumps(f"{self.provider}/{model}")
        edits = []  # (start, end, replacement), applied back to front

        node = root.get("model")
        if node is not None:
            edits.append((node.start, node.end, full))
        else:
            indent = jsonc.line_indent(text, root.start) + "  "
            edits.append((root.start + 1, root.start + 1, f'\n{indent}"model": {full},'))

        # models missing from the models.dev catalog must be declared or opencode refuses them
        models_node = root.get("provider", self.provider, "models")
        if model not in self._catalog_models() and not (models_node and model in models_node.children):
            entry = json.dumps({"name": model, "tool_call": True})
            if models_node is not None and models_node.kind == "object":
                indent = jsonc.line_indent(text, models_node.start) + "  "
                sep = "," if models_node.children else ""
                edits.append((models_node.start + 1, models_node.start + 1,
                              f"\n{indent}{json.dumps(model)}: {entry}{sep}"))
            # without a provider.models block opencode is relying on the catalog; leave the file alone there

        for start, end, rep in sorted(edits, reverse=True):
            text = text[:start] + rep + text[end:]
        jsonc.loads(text)  # must still parse
        return text

    def apply(self, model: str, dry_run=False) -> bool:
        text = self.path.read_text()
        new = self.render(text, model)
        if new == text:
            return False
        if not dry_run:
            backup(self.path)
            atomic_write(self.path, new)
        return True
