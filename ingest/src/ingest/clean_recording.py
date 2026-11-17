"""Turn a `playwright codegen --target python` recording into a skeleton that is
safe to share: the steps, in order, with nothing you typed or saw.

  - typed values (fill/type/press_sequentially) are dropped, not masked
  - numbers become #, words outside src/ingest/vocab.txt become ‹x›
    (--keep adds words you are happy to share, like the bank's brand name)
  - URLs keep the bank's host and the path shape only (ids -> :id)
  - anything not understood is listed by kind only, never its arguments

Read the output yourself before sharing it. Optionally pass --check with a JSON
file {"values": [...]} (like sensitive.json): if any of those values still
appear, nothing is printed.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from urllib.parse import urlsplit

import re

from .pdf_structure import MONTHS
from .scrub import SensitiveValueFound, blocking_check, scrub_name, url_pattern

# [data-test-id="card-number-input"]: attribute names and lowercase-hyphenated
# values are developer labels, kept as-is. Anything else in a selector (text,
# digits, mixed case) goes through scrub_name.
_ATTR = re.compile(r'\[\s*([a-z][a-z0-9_-]*)\s*([~|^$*]?=)\s*"([^"]*)"\s*\]')
_DEV_LABEL = re.compile(r"[a-z]+(?:[-_][a-z]+)*")

GETTERS = {
    "get_by_role", "get_by_label", "get_by_text", "get_by_placeholder", "get_by_test_id",
    "get_by_title", "get_by_alt_text", "locator", "frame_locator", "nth", "first", "last", "filter",
}
TYPED = {"fill", "type", "press_sequentially"}
ACTIONS = TYPED | {"click", "dblclick", "check", "uncheck", "press", "select_option", "set_input_files", "hover", "goto"}
WAITS = {"expect_download": "download", "expect_popup": "new tab", "expect_navigation": "navigation"}


class Cleaner:
    def __init__(self, keep: frozenset[str] = frozenset()):
        self.keep = frozenset(w.lower() for w in keep) | MONTHS

    def text(self, value: str) -> str:
        return f'"{scrub_name(value, self.keep)}"'

    def css(self, value: str) -> str:
        m = _ATTR.fullmatch(value.strip())
        if m:
            attr, op, val = m.groups()
            shown = val if _DEV_LABEL.fullmatch(val) else scrub_name(val, self.keep)
            return f'css [{attr}{op}"{shown}"]'
        # Other selectors can embed visible text (text=..., :has-text("...")) and ids.
        return f"css {scrub_name(value, self.keep | frozenset(('css', 'id', 'class', 'div', 'span', 'button', 'input', 'nth', 'child', 'text', 'has')))}"

    def url(self, value: str) -> str:
        parts = urlsplit(value)
        return f"{parts.scheme}://{parts.hostname}{url_pattern(value)}" if parts.hostname else url_pattern(value)

    def locator(self, node: ast.expr) -> str:
        chain = []
        while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in GETTERS:
            name, args = node.func.attr, node.args
            kw = {k.arg: k.value for k in node.keywords}
            s = lambda n: n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else None  # noqa: E731
            if name == "get_by_role":
                part = f"role={s(args[0]) if args else '?'}"
                if "name" in kw and s(kw["name"]) is not None:
                    part += f" {self.text(s(kw['name']))}"
            elif name in ("locator", "frame_locator"):
                part = ("frame " if name == "frame_locator" else "") + (self.css(s(args[0])) if args and s(args[0]) else "css ?")
            elif name in ("nth",):
                part = f"nth={args[0].value if args and isinstance(args[0], ast.Constant) else '?'}"
            elif name in ("first", "last"):
                part = name
            elif name == "filter":
                part = "filter" + (f" has_text {self.text(s(kw['has_text']))}" if "has_text" in kw and s(kw["has_text"]) else "")
            else:
                label = name.removeprefix("get_by_").replace("_", "")
                part = f"{label} {self.text(s(args[0]))}" if args and s(args[0]) is not None else label
            chain.append(part)
            node = node.func.value
        if isinstance(node, ast.Attribute) and node.attr in ("first", "last"):
            chain.append(node.attr)
            node = node.value
        tab = node.id if isinstance(node, ast.Name) else "?"
        return (f"[{tab}] " if tab not in ("page", "?") else "") + " > ".join(reversed(chain))

    def call(self, node: ast.Call) -> str | None:
        if not (isinstance(node.func, ast.Attribute)):
            return None
        action = node.func.attr
        if action not in ACTIONS:
            return None
        target = node.func.value
        if action == "goto":
            arg = node.args[0] if node.args else None
            return f"goto   {self.url(arg.value) if isinstance(arg, ast.Constant) else '?'}"
        where = self.locator(target)
        if action in TYPED:
            return f"{action:<6} {where}   [typed value removed]"
        if action == "press":
            key = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else "?"
            safe = key if key in {"Enter", "Tab", "Escape", "ArrowDown", "ArrowUp", "Space"} else "[key removed]"
            return f"press  {where}   {safe}"
        if action == "select_option":
            opt = node.args[0] if node.args else None
            return f"select {where}   {self.text(opt.value) if isinstance(opt, ast.Constant) and isinstance(opt.value, str) else '[option]'}"
        if action == "set_input_files":
            return f"upload {where}   [file removed]"
        return f"{action:<6} {where}"

    def statements(self, body: list[ast.stmt], depth: int = 0) -> list[str]:
        out = []
        pad = "    " * depth
        for st in body:
            if isinstance(st, ast.With):
                kinds = [
                    WAITS.get(i.context_expr.func.attr, "other")
                    for i in st.items
                    if isinstance(i.context_expr, ast.Call) and isinstance(i.context_expr.func, ast.Attribute)
                ]
                out.append(f"{pad}expect {', '.join(kinds) or 'something'}:")
                out += self.statements(st.body, depth + 1)
            elif isinstance(st, ast.FunctionDef):
                out += self.statements(st.body, depth)
            elif isinstance(st, (ast.Expr, ast.Assign)) and isinstance(st.value, ast.Call):
                line = self.call(st.value)
                if line:
                    out.append(pad + line)
                elif isinstance(st.value.func, ast.Attribute) and st.value.func.attr not in (
                    "close", "launch", "new_context", "new_page", "run", "stop", "wait_for_load_state",
                ):
                    out.append(f"{pad}[skipped: .{st.value.func.attr}(...)]")
        return out


def clean(source: str, keep: frozenset[str] = frozenset(), check_values: list[str] | None = None) -> str:
    tree = ast.parse(source)
    runs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run"]
    body = runs[0].body if runs else tree.body
    lines = Cleaner(keep).statements(body)
    text = "\n".join(f"{i:>3}  {line}" for i, line in enumerate(lines, 1))
    if check_values:
        blocking_check(text, check_values, [])
    return text


def main_clean(path: Path, keep: list[str], check: Path | None) -> int:
    values = json.loads(check.read_text()).get("values", []) if check else None
    try:
        print(clean(path.read_text(), frozenset(keep), values))
    except SensitiveValueFound as exc:
        print(f"stopped: a checked value is still in the output ({exc}); nothing printed")
        return 1
    except SyntaxError:
        print("not a Python recording: run codegen with --target python")
        return 2
    return 0
