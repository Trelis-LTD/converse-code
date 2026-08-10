"""Read Claude Code's JSONL session transcript.

The transcript (path arrives via the Stop hook payload) is the source of truth
for what Claude said and did — clean structured text, no ANSI. The watcher
tails it incrementally so the tool router can emit progress notes and detect
turn starts (including dev-typed ones).
"""

import json
from dataclasses import dataclass
from pathlib import Path

# Tools whose input names a file the turn touched.
FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _iter_entries(raw: str):
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _content_blocks(entry: dict) -> list[dict]:
    content = entry.get("message", {}).get("content")
    return content if isinstance(content, list) else []


@dataclass
class TurnSummary:
    text: str  # Claude's final prose for the turn (last text block)
    files: list[str]  # files edited/written this turn
    tools_used: list[str]


def read_new(path: str | Path, offset: int) -> tuple[list[dict], int]:
    """Entries appended since byte `offset`, and the new offset."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            raw = f.read()
    except FileNotFoundError:
        return [], offset
    return list(_iter_entries(raw.decode("utf-8", "replace"))), offset + len(raw)


def summarize_entries(entries: list[dict]) -> TurnSummary:
    text = ""
    files: list[str] = []
    tools: list[str] = []
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        for block in _content_blocks(entry):
            if block.get("type") == "text" and block.get("text", "").strip():
                text = block["text"].strip()
            elif block.get("type") == "tool_use":
                name = block.get("name", "")
                tools.append(name)
                file_path = block.get("input", {}).get("file_path")
                if name in FILE_TOOLS and file_path and file_path not in files:
                    files.append(file_path)
    return TurnSummary(text=text, files=files, tools_used=tools)


def milestone(entry: dict) -> dict | None:
    """Classify one transcript entry for narration.

    Cadence rule: milestones speak, telemetry stays silent. A test run is worth
    interrupting silence for; a file edit is a silent partial that keeps the
    voice current for "how's it going?"; everything else is a progress note.
    """
    if entry.get("type") != "assistant":
        return None
    for block in _content_blocks(entry):
        if block.get("type") != "tool_use":
            continue
        name = block.get("name", "")
        inp = block.get("input", {})
        if name in FILE_TOOLS:
            target = Path(inp.get("file_path", "")).name or "a file"
            return {"kind": "edit", "speak": f"Edited {target}.", "files": [target]}
        if name == "Bash":
            desc = (inp.get("description") or "")[:120]
            if "test" in desc.lower():
                return {"kind": "tests", "speak": "Running the tests now."}
            return {"kind": "note", "note": desc.lower() if desc else "running a command"}
        if name in ("Read", "Grep", "Glob"):
            return {"kind": "note", "note": "reading the code"}
    # Claude's own interstitial prose is the best account of what it is doing
    # and why — feed it as silent telemetry so "how's it going?" answers from
    # Claude's narration, not just activity labels. Never spoken unprompted.
    for block in _content_blocks(entry):
        if block.get("type") == "text":
            text = " ".join(str(block.get("text") or "").split())
            if len(text) >= 12:
                return {"kind": "note", "note": speak_summary(text, 200)}
    return None


def speak_summary(text: str, limit: int = 240) -> str:
    """Compress final prose to a short speakable summary (sentence-aware cut)."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    for sep in (". ", "! ", "? "):
        idx = cut.rfind(sep)
        if idx > limit // 3:
            return cut[: idx + 1]
    return cut.rsplit(" ", 1)[0] + "…"
