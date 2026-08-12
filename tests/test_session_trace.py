import json
import stat

from converse_code.session_trace import SessionTrace


def test_session_trace_writes_timestamped_jsonl_and_redacts_credentials(tmp_path):
    path = tmp_path / "session.jsonl"
    trace = SessionTrace(path)
    trace.record(
        "browser",
        "credential_received",
        api_key="ck_never-write-this",
        nested={"session_token": "secret-token", "command": "pwd"},
        url="http://127.0.0.1:8737/?t=local-secret",
        command=(
            "OPENAI_API_KEY=sk-inline-secret --token ghp_inline_secret "
            "--password do-not-write"
        ),
    )
    trace.close()

    entry = json.loads(path.read_text().strip())
    assert entry["source"] == "browser"
    assert entry["event"] == "credential_received"
    assert entry["timestamp"].endswith("Z")
    assert entry["session_id"]
    assert entry["data"]["api_key"] == "[REDACTED]"
    assert entry["data"]["nested"] == {
        "session_token": "[REDACTED]", "command": "pwd",
    }
    assert entry["data"]["url"] == "http://127.0.0.1:8737/?t=[REDACTED]"
    assert entry["data"]["command"] == (
        "OPENAI_API_KEY=[REDACTED] --token [REDACTED] --password [REDACTED]"
    )
    contents = path.read_text()
    assert "ck_never-write-this" not in contents
    assert "sk-inline-secret" not in contents
    assert "ghp_inline_secret" not in contents
    assert "do-not-write" not in contents
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_session_trace_appends_distinct_sessions(tmp_path):
    path = tmp_path / "session.jsonl"
    first = SessionTrace(path)
    first.record("host", "session_start")
    first.close()
    second = SessionTrace(path)
    second.record("host", "session_start")
    second.close()

    entries = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(entries) == 2
    assert entries[0]["session_id"] != entries[1]["session_id"]
