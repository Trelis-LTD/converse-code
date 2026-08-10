import asyncio
import json

import pytest


class FakeDriver:
    """Stands in for ClaudeHost: records injections/keys, screen is settable."""

    def __init__(self):
        self.lines = [" > ", ""]
        self.injected = []
        self.keys = []

    def snapshot(self):
        return list(self.lines)

    def inject(self, text):
        self.injected.append(text)

    def send_key(self, name):
        self.keys.append(name)


class FakeSender:
    """Stands in for BrokerClient's tool senders."""

    def __init__(self):
        self.results = []
        self.progress = []
        self.context = []
        self.deferred = []
        self.partials = []

    async def send_tool_result(self, call_id, content):
        self.results.append((call_id, content))

    async def send_tool_progress(self, call_id, note):
        self.progress.append((call_id, note))

    async def send_tool_deferred(self, call_id, handle, status_label=None):
        self.deferred.append((call_id, handle))

    async def send_tool_partial_result(self, call_id, content, reply=False):
        self.partials.append((call_id, content, reply))

    async def send_context(self, text, role="context", reply=False):
        self.context.append((text, role, reply))


@pytest.fixture
def fake_driver():
    return FakeDriver()


@pytest.fixture
def fake_sender():
    return FakeSender()


@pytest.fixture
def router(fake_driver, fake_sender, tmp_path):
    from converse_code.tools import ToolRouter

    r = ToolRouter(fake_driver, fake_sender, handle="cc-test-abc", project_dir=tmp_path)
    r.HOLD_S = 1.5
    r.POLL_S = 0.05
    r.SETTLE_S = 0.05
    r.transcript_path = tmp_path / "session.jsonl"
    r.transcript_path.write_text("")
    return r


def append_transcript(router, *entries):
    with open(router.transcript_path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


async def finish_turn(router, delay=0.15, transcript_path=None):
    await asyncio.sleep(delay)
    await router.on_hook("stop", {"transcript_path": str(transcript_path or router.transcript_path)})
