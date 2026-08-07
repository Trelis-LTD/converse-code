"""Menu detection from the rendered terminal screen.

Reads the pyte screen buffer for *structure only* — which menu is open, which
options exist, which is selected. Claude Code's prose never comes from here
(that's the JSONL transcript); this exists because menus, permission prompts,
and trust dialogs render only on screen and never touch the transcript.

Calibrated against the real Claude Code TUI, which is full of traps:
- The *idle input prompt* is also a "❯", sitting between two horizontal rules —
  that must never read as a menu.
- Menus render inside box-drawing borders (│ … │), so borders are stripped
  before structural matching.
"""

import re
from dataclasses import dataclass, field

CURSOR = "❯"

# "❯ 1. Yes" / "  2. No, and tell Claude what to do differently (esc)"
NUMBERED_RE = re.compile(r"^\s*(?P<cursor>❯)?\s*(?P<num>\d+)\.\s+(?P<label>\S.*?)\s*$")
RULE_RE = re.compile(r"^\s*[─━═╭╰╮╯]+\s*$")


@dataclass
class Menu:
    title: str
    options: list[str] = field(default_factory=list)
    selected: int = 0  # index into options


def _clean(line: str) -> str:
    """Strip box-drawing borders so structure regexes see the content."""
    return line.replace("│", " ").rstrip()


def _is_decor(line: str) -> bool:
    stripped = _clean(line).strip()
    return not stripped or bool(RULE_RE.match(stripped))


def detect_menu(lines: list[str]) -> Menu | None:
    """Return the open menu, or None (including for the idle ❯ input prompt)."""
    cleaned = [_clean(l) for l in lines]

    numbered = [(i, m) for i, l in enumerate(cleaned) if (m := NUMBERED_RE.match(l))]
    cursor_on_numbered = [n for n, (_, m) in enumerate(numbered) if m.group("cursor")]
    if cursor_on_numbered:
        # A numbered menu only counts when the cursor sits on one of its rows —
        # numbered lists in Claude's prose plus the prompt ❯ must not match.
        options = [m.group("label") for _, m in numbered]
        return Menu(
            title=_title_above(cleaned, numbered[0][0]),
            options=options,
            selected=cursor_on_numbered[0],
        )

    for i, line in enumerate(cleaned):
        if CURSOR not in line:
            continue
        if _neighbor_is_rule(cleaned, i):
            continue  # the idle input prompt: rule / ❯ … / rule
        # Submitted prompts remain in Claude's scrollback with the same cursor
        # glyph. Their next rendered line is a response/status marker, whereas
        # live unnumbered picker rows are followed by another plain option.
        next_line = cleaned[i + 1].lstrip() if i + 1 < len(cleaned) else ""
        if next_line.startswith(("⎿", "⏺", "✻", "·")):
            continue
        # Unnumbered menu (e.g. /model picker): contiguous non-decor block.
        start = i
        while start > 0 and not _is_decor(cleaned[start - 1]) \
                and not cleaned[start - 1].rstrip().endswith((":", "?")):
            start -= 1
        end = i
        while end + 1 < len(cleaned) and not _is_decor(cleaned[end + 1]):
            end += 1
        block = cleaned[start : end + 1]
        if len(block) < 2:
            continue
        options = [l.replace(CURSOR, " ").strip() for l in block]
        return Menu(title=_title_above(cleaned, start), options=options, selected=i - start)
    return None


def _neighbor_is_rule(lines: list[str], idx: int) -> bool:
    above = lines[idx - 1].strip() if idx > 0 else ""
    below = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
    return bool(RULE_RE.match(above)) or bool(RULE_RE.match(below))


def _title_above(lines: list[str], first_option_line: int) -> str:
    for i in range(first_option_line - 1, -1, -1):
        if not _is_decor(lines[i]):
            return lines[i].strip()
    return ""


def match_option(menu: Menu, wanted: str) -> int | None:
    """Index of the option best matching `wanted` (case-insensitive), else None."""
    wanted_l = wanted.strip().lower()
    if not wanted_l:
        return None
    # Exact, then prefix, then substring — first hit wins within each tier.
    lowered = [o.lower() for o in menu.options]
    for pred in (
        lambda o: o == wanted_l,
        lambda o: o.startswith(wanted_l),
        lambda o: wanted_l in o,
    ):
        for i, o in enumerate(lowered):
            if pred(o):
                return i
    if wanted_l.isdigit():  # "option 2" spoken as a number
        n = int(wanted_l)
        if 1 <= n <= len(menu.options):
            return n - 1
    return None
