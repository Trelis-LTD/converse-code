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


def test_milestone_classifies_entries():
    edit = tmod.milestone(entry_assistant(tool_use("Edit", file_path="/p/auth.py")))
    assert edit == {"kind": "edit", "speak": "Edited auth.py.", "files": ["auth.py"]}
    tests = tmod.milestone(entry_assistant(tool_use("Bash", description="Run test suite")))
    assert tests == {"kind": "tests", "speak": "Running the tests now."}
    # Claude's interstitial prose becomes silent telemetry, compressed
    prose = tmod.milestone(entry_assistant(text("README created. Now building the game itself.")))
    assert prose["kind"] == "note"
    assert "building the game" in prose["note"]
    assert tmod.milestone(entry_assistant(text("Done."))) is None  # too short to be informative
    assert tmod.milestone({"type": "user"}) is None


def test_milestone_ignores_non_assistant_and_plain_bash():
    note = tmod.milestone(entry_assistant(tool_use("Bash", description="Install deps")))
    assert note == {"kind": "note", "note": "install deps"}
    assert tmod.milestone(entry_assistant(tool_use("Grep", pattern="x"))) == {
        "kind": "note", "note": "reading the code"}


def test_milestone_test_detection_needs_word_boundary():
    assert tmod.milestone(entry_assistant(
        tool_use("Bash", description="Show latest commit")))["kind"] == "note"
    assert tmod.milestone(entry_assistant(
        tool_use("Bash", description="Run the test suite")))["kind"] == "tests"


def test_milestone_priority_across_parallel_blocks():
    """Parallel tool calls share one entry; a Read block must not mask a test
    run, and multiple edits merge into one partial."""
    entry = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {}},
        {"type": "tool_use", "name": "Bash", "input": {"description": "Run tests"}},
    ]}}
    assert tmod.milestone(entry)["kind"] == "tests"
    edits = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/p/a.py"}},
        {"type": "tool_use", "name": "Write", "input": {"file_path": "/p/b.py"}},
    ]}}
    m = tmod.milestone(edits)
    assert m == {"kind": "edit", "speak": "Edited 2 files.", "files": ["a.py", "b.py"]}


def test_speak_summary_cuts_at_sentence():
    long = "First sentence is here. " + "word " * 100
    out = tmod.speak_summary(long, limit=60)
    assert out == "First sentence is here."
    assert tmod.speak_summary("short") == "short"
