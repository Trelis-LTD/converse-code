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
from dataclasses import dataclass

CURSOR = "❯"

# "❯ 1. Yes" / "  2. No, and tell Claude what to do differently (esc)"
NUMBERED_RE = re.compile(
    r"^\s*(?P<cursor>❯)?\s*[✻✽✶●·*]?\s*(?P<num>\d+)\.\s+(?P<label>\S.*?)\s*$"
)
RULE_RE = re.compile(r"^\s*[─━═╭╰╮╯]+\s*$")
STATUS_MARKER_RE = re.compile(r"^[✻✽✶✢●·*]\s+")
EMPTY_PLACEHOLDER_RE = re.compile(
    r'^\s*❯\s*Try\s+["“].+["”]\s*$', re.IGNORECASE,
)
SET_MODEL_RE = re.compile(
    r"\bSet model to (?P<model>Default|Opus|Fable|Sonnet|Haiku)\b", re.IGNORECASE,
)
STATUS_MODEL_RE = re.compile(
    r"\b(?P<model>Default|Opus|Fable|Sonnet|Haiku)(?:\s+\d+(?:\.\d+)?)?\s+with\s+"
    r"(?:low|medium|high|max)\s+effort\b",
    re.IGNORECASE,
)
HEADER_MODEL_RE = re.compile(
    r"^\s*(?P<model>Default|Opus|Fable|Sonnet|Haiku)(?:\s+\d+(?:\.\d+)?)?"
    r"\s+·\s+Claude(?:\s|$)",
    re.IGNORECASE,
)
KEPT_MODEL_RE = re.compile(
    r"\bKept model as (?P<model>Default|Opus|Fable|Sonnet|Haiku)\b", re.IGNORECASE,
)


@dataclass(frozen=True)
class Menu:
    title: str
    options: tuple[str, ...]
    selected: int

    def __post_init__(self) -> None:
        if not self.options or not 0 <= self.selected < len(self.options):
            raise ValueError("a menu needs options and an in-range selection")


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
        selected_row = numbered[cursor_on_numbered[0]][0]
        # A newer live composer below the numbered rows proves this menu is scrollback, not a
        # blocking control. Never press Enter based on historical menu text.
        if any(
            CURSOR in cleaned[i] and _neighbor_is_rule(cleaned, i)
            for i in range(selected_row + 1, len(cleaned))
        ):
            cursor_on_numbered = []
    if cursor_on_numbered:
        # A numbered menu only counts when the cursor sits on one of its rows —
        # numbered lists in Claude's prose plus the prompt ❯ must not match.
        options = [m.group("label") for _, m in numbered]
        return Menu(
            title=_title_above(cleaned, numbered[0][0]),
            options=tuple(options),
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
        title = _title_above(cleaned, start)
        # Claude's only supported unnumbered control is the model picker.
        # Prompt history can otherwise resemble an arbitrary option list.
        if "model" not in title.lower():
            continue
        options = [l.replace(CURSOR, " ").strip() for l in block]
        return Menu(title=title, options=tuple(options), selected=i - start)
    return None


def menu_context(lines: list[str], menu: Menu) -> list[str]:
    """Return the current menu's rendered block without scrollback above its boundary."""
    expected = set(menu.options)
    option_rows = []
    for index, line in enumerate(lines):
        match = NUMBERED_RE.match(_clean(line))
        if match and match.group("label") in expected:
            option_rows.append(index)
    if not option_rows:
        return []
    start = min(option_rows)
    while start > 0:
        previous = _clean(lines[start - 1])
        if RULE_RE.match(previous):
            break
        if previous.lstrip().startswith(CURSOR) and not NUMBERED_RE.match(previous):
            break
        start -= 1
    end = max(option_rows) + 1
    while end < len(lines) and not _clean(lines[end]).strip():
        end += 1
    return lines[start:end]


def is_model_scope_prompt(lines: list[str]) -> bool:
    """Whether Claude is asking if a selected model applies by default or this session."""
    # Search the complete rendered screen: on short terminals the explanatory
    # footer may wrap far enough away from the cursor to fall outside a small
    # tail window. Requiring every key hint keeps this specific to the
    # single-key model-scope prompt rather than generic prose about models.
    visible = " ".join(_clean(line).lower() for line in lines)
    complete_prompt = all(
        phrase in visible
        for phrase in (
            "enter to set as default",
            "s to use this session only",
            "esc to cancel",
        )
    )
    # Claude's live status redraw can temporarily overwrite characters in the
    # footer (observed as "sessio  only · Esc to cancel" in 2.1.227). The
    # surviving suffix is still distinctive and, critically, still blocking.
    redraw_fragment = (
        "sessio" in visible
        and "only" in visible
        and "cancel" in visible
    )
    return complete_prompt or redraw_fragment


def detect_model(lines: list[str]) -> str | None:
    """Read Claude's visible model status without treating arbitrary prose as state."""
    for line in reversed(lines):
        cleaned = _clean(line)
        match = (
            SET_MODEL_RE.search(cleaned)
            or KEPT_MODEL_RE.search(cleaned)
            or STATUS_MODEL_RE.search(cleaned)
            or HEADER_MODEL_RE.search(cleaned)
        )
        if match:
            return match.group("model").lower()
    return None


def detect_current_model(lines: list[str]) -> str | None:
    """Read only Claude's current model header or live picker selection.

    Unlike ``detect_model``, this deliberately ignores result rows in scrollback. Those rows are
    useful history, but cannot prove the model selected for the current invocation.
    """
    menu = detect_menu(lines)
    if menu and "model" in menu.title.lower() and menu.options:
        selected = menu.options[menu.selected]
        match = re.search(r"\b(Default|Opus|Fable|Sonnet|Haiku)\b", selected, re.IGNORECASE)
        if match:
            return match.group(1).lower()
    return detect_header_model(lines)


def detect_header_model(lines: list[str]) -> str | None:
    """Read only the active Claude model header, never a picker row or result history."""
    for line in reversed(lines):
        cleaned = _clean(line)
        if match := HEADER_MODEL_RE.search(cleaned):
            return match.group("model").lower()
        # Welcome/current status header used by recent Claude builds. Requiring the Claude
        # product suffix prevents ordinary transcript prose about effort from becoming state.
        if "· Claude" in cleaned and (match := STATUS_MODEL_RE.search(cleaned)):
            return match.group("model").lower()
    return None


def has_empty_composer(lines: list[str], *, allow_stale_scope: bool = False) -> bool:
    """Whether the latest prompt row is an active, structurally empty composer."""
    cleaned = [_clean(line) for line in lines]
    prompt_rows = [
        i for i, line in enumerate(cleaned)
        if line.lstrip().startswith(CURSOR)
    ]
    if not prompt_rows:
        return False
    index = prompt_rows[-1]
    prompt = cleaned[index]
    if prompt.strip() != CURSOR and not EMPTY_PLACEHOLDER_RE.match(prompt):
        return False
    # Historical prompt rows have transcript/output below them. A live composer
    # is followed only by Ink rules or its shortcut footer.
    tail = cleaned[index + 1:]
    if allow_stale_scope and is_model_scope_prompt(tail):
        return True
    for line in tail:
        stripped = line.strip()
        lowered = stripped.lower()
        if (
            not stripped
            or RULE_RE.match(line)
            or "? for shortcuts" in lowered
            or "mode on" in lowered
            or "cooked" in lowered
            or STATUS_MARKER_RE.match(stripped)
            # Claude's interrupted repaint can interleave a horizontal rule with its footer,
            # producing fragments such as "manual mod──on" and "Esc─again to─clear".
            # Require substantial rule chrome as well as footer vocabulary so ordinary response
            # prose containing these words is not mistaken for an idle composer.
            or (
                line.count("─") >= 4
                and (
                    "manual mod" in lowered
                    or ("esc" in lowered and "clear" in lowered)
                )
            )
        ):
            continue
        return False
    return True


def is_idle(lines: list[str]) -> bool:
    """Whether Claude's empty composer is ready and no scope picker blocks it."""
    return not is_model_scope_prompt(lines) and has_empty_composer(lines)


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
    if wanted_l in {"default", "opus", "fable", "sonnet", "haiku"}:
        for i, option in enumerate(menu.options):
            label = re.split(r"\s{2,}", option.strip(), maxsplit=1)[0]
            compact = re.sub(r"\s+", "", label.lower().replace("✔", ""))
            if _within_one_edit(compact, wanted_l):
                return i
    if wanted_l.isdigit():  # "option 2" spoken as a number
        n = int(wanted_l)
        if 1 <= n <= len(menu.options):
            return n - 1
    return None


def _within_one_edit(value: str, target: str) -> bool:
    """True for one insertion, deletion, or substitution (small labels only)."""
    if abs(len(value) - len(target)) > 1:
        return False
    if len(value) == len(target):
        return sum(a != b for a, b in zip(value, target)) <= 1
    shorter, longer = (value, target) if len(value) < len(target) else (target, value)
    i = j = differences = 0
    while i < len(shorter) and j < len(longer):
        if shorter[i] == longer[j]:
            i += 1
            j += 1
        else:
            differences += 1
            j += 1
            if differences > 1:
                return False
    return True
