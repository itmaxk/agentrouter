"""Just enough JSONC parsing to locate value spans, so edits keep comments and layout intact."""
import json
import re


class Node:
    def __init__(self, start, end, kind, children=None, value=None):
        self.start, self.end, self.kind = start, end, kind   # end is exclusive
        self.children = children or {}                     # key -> Node for objects
        self.value = value

    def get(self, *path):
        node = self
        for key in path:
            if node is None or node.kind != "object":
                return None
            node = node.children.get(key)
        return node


class _Parser:
    def __init__(self, text):
        self.s, self.i = text, 0

    def ws(self):
        s = self.s
        while self.i < len(s):
            c = s[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif s.startswith("//", self.i):
                nl = s.find("\n", self.i)
                self.i = len(s) if nl < 0 else nl + 1
            elif s.startswith("/*", self.i):
                self.i = s.index("*/", self.i) + 2
            else:
                break

    def string(self):
        m = re.compile(r'"(?:[^"\\]|\\.)*"').match(self.s, self.i)
        if not m:
            raise ValueError(f"expected string at {self.i}")
        self.i = m.end()
        return Node(m.start(), m.end(), "string", value=json.loads(m.group()))

    def value(self):
        self.ws()
        c = self.s[self.i]
        if c == "{":
            return self.container("}", obj=True)
        if c == "[":
            return self.container("]", obj=False)
        if c == '"':
            return self.string()
        m = re.compile(r"-?[\w.+-]+").match(self.s, self.i)
        if not m:
            raise ValueError(f"unexpected {c!r} at {self.i}")
        self.i = m.end()
        return Node(m.start(), m.end(), "scalar", value=m.group())

    def container(self, close, obj):
        start = self.i
        self.i += 1
        children, items = {}, []
        while True:
            self.ws()
            if self.s[self.i] == close:
                self.i += 1
                break
            if obj:
                key = self.string().value
                self.ws()
                if self.s[self.i] != ":":
                    raise ValueError(f"expected ':' at {self.i}")
                self.i += 1
                children[key] = self.value()
            else:
                items.append(self.value())
            self.ws()
            if self.s[self.i] == ",":
                self.i += 1
        return Node(start, self.i, "object" if obj else "array", children=children if obj else None, value=items or None)


def parse(text: str) -> Node:
    p = _Parser(text)
    node = p.value()
    return node


def loads(text: str):
    """Strict-ish load: drop comments and trailing commas, then json.loads."""
    out, p = [], _Parser(text)
    i = 0
    while i < len(text):
        if text[i] == '"':
            p.i = i
            n = p.string()
            out.append(text[n.start:n.end])
            i = n.end
        elif text.startswith("//", i) or text.startswith("/*", i):
            p.i = i
            p.ws()
            i = p.i
        else:
            out.append(text[i])
            i += 1
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))


def line_indent(text: str, pos: int) -> str:
    line_start = text.rfind("\n", 0, pos) + 1
    m = re.match(r"[ \t]*", text[line_start:])
    return m.group()
