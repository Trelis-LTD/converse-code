import asyncio
import json

import pytest


class FakeDriver:
    """Stands in for ClaudeHost: records injections/keys, screen is settable."""

    def __init__(self):
        self.lines = ["────", "❯", "────"]
        self.injected = []
        self.keys = []
        self.router = None
        self.auto_ack = True
        self._prompt_id = 0

    def snapshot(self):
        return list(self.lines)

    def inject(self, text, submit_delay_s=None):
        self.injected.append(text)
        if self.router is not None and self.auto_ack and not text.startswith("/"):
            self._prompt_id += 1
            asyncio.create_task(self.router.on_hook("user_prompt_submit", {
                "prompt": text, "prompt_id": f"test-prompt-{self._prompt_id}",
            }))

    def inject_command(self, text, submit_delay_s=None):
        self.injected.append(text)

    def send_key(self, name):
        self.keys.append(name)
        if name not in {"up", "down"}:
            return
        # Model the TUI repaint that production navigation now requires before sending the next
        # key. Lifecycle tests can still replace the whole screen to model a closing modal.
        from converse_code.screen import detect_menu

        menu = detect_menu(self.lines)
        if menu is None:
            return
        target = max(0, min(len(menu.options) - 1, menu.selected + (1 if name == "down" else -1)))
        if target == menu.selected:
            return
        option_rows = []
        for option in menu.options:
            option_rows.append(next(
                i for i, line in enumerate(self.lines)
                if option in line and ("❯" in line or line.strip().lstrip("0123456789. ") == option)
            ))
        old_row, new_row = option_rows[menu.selected], option_rows[target]
        self.lines[old_row] = self.lines[old_row].replace("❯", " ", 1)
        stripped = self.lines[new_row].lstrip()
        self.lines[new_row] = " ❯ " + stripped


class FakeSender:
    """Stands in for BrokerClient's tool senders."""

    def __init__(self):
        self.results = []
        self.result_metadata = []
        self.progress = []
        self.context = []
        self.deferred = []
        self.partials = []

    async def send_tool_result(self, call_id, content, **metadata):
        self.results.append((call_id, content))
        self.result_metadata.append(metadata)

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
    fake_driver.router = r
    r.HOLD_S = 1.5
    r.POLL_S = 0.05
    r.SETTLE_S = 0.05
    r.READY_POLL_S = 0.01
    r.READY_TIMEOUT_S = 0.2
    r.transcript_path = tmp_path / "session.jsonl"
    r.transcript_path.write_text("")
    return r


def append_transcript(router, *entries):
    with open(router.transcript_path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


async def finish_turn(router, delay=0.15, transcript_path=None):
    await asyncio.sleep(delay)
    prompt_id = next(iter(router._episode_prompt_ids), None)
    await router.on_hook("stop", {
        "transcript_path": str(transcript_path or router.transcript_path),
        "prompt_id": prompt_id,
    })
