import json

from converse_code import transcript as tmod


def entry_assistant(*blocks):
    return {"type": "assistant", "message": {"content": list(blocks)}}


def text(t):
    return {"type": "text", "text": t}


def tool_use(name, **inp):
    return {"type": "tool_use", "name": name, "input": inp}


def write_jsonl(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def test_read_new_is_incremental(tmp_path):
    path = tmp_path / "t.jsonl"
    write_jsonl(path, [entry_assistant(text("one"))])
    entries, offset = tmod.read_new(path, 0)
    assert len(entries) == 1

    with open(path, "a") as f:
        f.write(json.dumps(entry_assistant(text("two"))) + "\n")
    entries, offset2 = tmod.read_new(path, offset)
    assert [e["message"]["content"][0]["text"] for e in entries] == ["two"]
    assert offset2 > offset

    entries, _ = tmod.read_new(path, offset2)
    assert entries == []


def test_summarize_collects_last_text_and_files():
    entries = [
        entry_assistant(text("Let me look."), tool_use("Read", file_path="/p/auth.py")),
        entry_assistant(tool_use("Edit", file_path="/p/auth.py")),
        entry_assistant(tool_use("Write", file_path="/p/tests/test_auth.py")),
        {"type": "user", "message": {"content": "irrelevant"}},
        entry_assistant(text("Fixed the bug and added a test. All tests pass.")),
    ]
    s = tmod.summarize_entries(entries)
    assert s.text == "Fixed the bug and added a test. All tests pass."
    assert s.files == ["/p/auth.py", "/p/tests/test_auth.py"]
    assert "Edit" in s.tools_used


def test_progress_notes():
    assert tmod.progress_note(entry_assistant(tool_use("Edit", file_path="/p/auth.py"))) == "editing auth.py"
    assert tmod.progress_note(entry_assistant(tool_use("Bash", description="Run test suite"))) == "run test suite"
    assert tmod.progress_note(entry_assistant(tool_use("Grep", pattern="x"))) == "reading the code"
    assert tmod.progress_note(entry_assistant(text("hi"))) is None
    assert tmod.progress_note({"type": "user"}) is None


def test_speak_summary_cuts_at_sentence():
    long = "First sentence is here. " + "word " * 100
    out = tmod.speak_summary(long, limit=60)
    assert out == "First sentence is here."
    assert tmod.speak_summary("short") == "short"
