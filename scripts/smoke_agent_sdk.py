#!/usr/bin/env python3
"""One real-Claude SDK workflow covering files, model control, and a browser game."""

import asyncio
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

ROOT = Path(__file__).resolve().parents[1]


WORKFLOW_BUDGET_USD = 5.0
WORKFLOW_TURNS = 60
PER_QUERY_BUDGET_USD = 1.0
PER_QUERY_TURNS = 10


class WorkflowUsage:
    def __init__(self) -> None:
        self.cost_usd = 0.0
        self.turns = 0

    def reserve_query(self) -> None:
        if self.cost_usd + PER_QUERY_BUDGET_USD > WORKFLOW_BUDGET_USD:
            raise RuntimeError("SDK workflow has no remaining query budget")
        if self.turns + PER_QUERY_TURNS > WORKFLOW_TURNS:
            raise RuntimeError("SDK workflow has no remaining turn budget")

    def record(self, result: ResultMessage) -> None:
        if result.total_cost_usd is None:
            raise RuntimeError("SDK result omitted cost usage; cannot enforce workflow budget")
        self.cost_usd += result.total_cost_usd
        self.turns += result.num_turns
        if self.cost_usd > WORKFLOW_BUDGET_USD or self.turns > WORKFLOW_TURNS:
            raise RuntimeError(
                f"SDK workflow exceeded budget: ${self.cost_usd:.4f}, {self.turns} turns"
            )


async def _ask(
    client: ClaudeSDKClient, prompt: str,
) -> tuple[str, set[str], ResultMessage, list[ToolUseBlock], set[str]]:
    print(f"SDK prompt: {prompt}", flush=True)
    await client.query(prompt)
    text: list[str] = []
    models: set[str] = set()
    tool_uses: list[ToolUseBlock] = []
    successful_tool_ids: set[str] = set()
    result_message: ResultMessage | None = None
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            models.add(message.model)
            text.extend(block.text for block in message.content if isinstance(block, TextBlock))
            tool_uses.extend(block for block in message.content if isinstance(block, ToolUseBlock))
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            successful_tool_ids.update(
                block.tool_use_id
                for block in message.content
                if isinstance(block, ToolResultBlock) and block.is_error is not True
            )
        elif isinstance(message, ResultMessage):
            result_message = message
            if message.is_error:
                raise RuntimeError(f"Claude SDK turn failed: {message}")
    if result_message is None:
        raise RuntimeError("Claude SDK turn ended without a result")
    answer = "\n".join(text).strip()
    print(f"SDK response: {answer}", flush=True)
    return answer, models, result_message, tool_uses, successful_tool_ids


async def ask(
    client: ClaudeSDKClient, prompt: str, usage: WorkflowUsage,
) -> tuple[str, set[str], ResultMessage, list[ToolUseBlock], set[str]]:
    usage.reserve_query()
    answer, models, result, tool_uses, successful_tool_ids = await asyncio.wait_for(
        _ask(client, prompt), timeout=240,
    )
    usage.record(result)
    return answer, models, result, tool_uses, successful_tool_ids


def require_tool(
    tool_uses: list[ToolUseBlock], successful_tool_ids: set[str], name: str,
    project: Path, target: str | None = None,
) -> None:
    path_key = {"Read": "file_path", "Write": "file_path", "Edit": "file_path", "Glob": "path"}.get(name)
    for call in tool_uses:
        if call.name != name:
            continue
        if call.id not in successful_tool_ids:
            continue
        if target is None:
            return
        raw = call.input.get(path_key) if path_key else None
        if not isinstance(raw, str):
            continue
        candidate = Path(raw)
        resolved = candidate.resolve() if candidate.is_absolute() else (project / candidate).resolve()
        if resolved == (project / target).resolve():
            return
    raise RuntimeError(f"turn did not use {name} on {target or 'the required target'}")


def require_review_sections(text: str) -> None:
    headings = ("Accessibility", "Security", "Correctness", "Maintainability")
    matches = list(re.finditer(r"(?im)^#{1,6}\s+(.+?)\s*$", text))
    sections: dict[str, list[str]] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        normalized = re.sub(r"^\d+[.)]\s*", "", match.group(1)).strip().casefold()
        sections.setdefault(normalized, []).append(text[match.end():end].strip())
    missing = [
        heading for heading in headings
        if not any(len(body) >= 40 for body in sections.get(heading.casefold(), []))
    ]
    if missing:
        raise RuntimeError(f"REVIEW.md lacks substantive sections: {', '.join(missing)}")


async def inspect_in_chromium(path: Path, *, text: str | None = None, game: bool = False) -> None:
    from playwright.async_api import async_playwright

    errors: list[str] = []
    external_requests: list[str] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            async def contain_request(route):
                if route.request.url.startswith(("file:", "data:", "about:")):
                    await route.continue_()
                else:
                    external_requests.append(route.request.url)
                    await route.abort()

            await page.route("**/*", contain_request)
            await page.add_init_script("""
              for (const name of ['WebSocket', 'RTCPeerConnection', 'webkitRTCPeerConnection']) {
                Object.defineProperty(globalThis, name, {
                  configurable: true,
                  value: class BlockedExternalChannel {
                    constructor() { throw new Error(`${name} is disabled in the isolated eval`); }
                  },
                });
              }
            """)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            await page.goto(path.resolve().as_uri())
            if text is not None and text not in await page.locator("body").inner_text():
                raise RuntimeError(f"Chromium did not render expected text from {path.name}")
            if game:
                await page.locator("#start").click()
                before = await page.locator("#score").inner_text()
                before_samples = []
                for _ in range(3):
                    await page.wait_for_timeout(500)
                    before_samples.append(await page.locator("#score").inner_text())
                await page.locator("#target").click()
                after = await page.locator("#score").inner_text()
                after_samples = []
                for _ in range(3):
                    await page.wait_for_timeout(500)
                    after_samples.append(await page.locator("#score").inner_text())
                before_numbers = re.findall(r"-?\d+", before)
                after_numbers = re.findall(r"-?\d+", after)
                if (
                    len(before_numbers) != 1
                    or len(after_numbers) != 1
                    or int(after_numbers[0]) != int(before_numbers[0]) + 1
                    or any(sample != before for sample in before_samples)
                    or any(sample != after for sample in after_samples)
                ):
                    raise RuntimeError(
                        "score must stay stable except for exactly one target-click point; "
                        f"observed {before!r}, {before_samples!r}, {after!r}, {after_samples!r}"
                    )
            if errors:
                raise RuntimeError(f"browser errors for {path.name}: {errors}")
            if external_requests:
                raise RuntimeError(
                    f"external browser requests for {path.name}: {external_requests}"
                )
        finally:
            await browser.close()


def current_global_model(settings: Path) -> object:
    if not settings.exists():
        return None
    return json.loads(settings.read_text()).get("model")


async def main() -> None:
    temp_parent = ROOT / "tmp"
    temp_parent.mkdir(exist_ok=True)
    global_settings = Path.home() / ".claude/settings.json"
    model_before = current_global_model(global_settings)

    try:
        with tempfile.TemporaryDirectory(prefix="cc-sdk-workflow-", dir=temp_parent) as name:
            project = Path(name).resolve()
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            (project / "initial.txt").write_text("workspace-boundary\n")
            sdk_settings = project / ".sdk-settings.json"
            sdk_settings.write_text("{}\n")

            async def guard(tool: str, input_: dict, _context):
                path_key = {"Read": "file_path", "Write": "file_path", "Edit": "file_path", "Glob": "path", "Grep": "path"}.get(tool)
                if path_key is None:
                    return PermissionResultDeny(message=f"Tool not allowed: {tool}", interrupt=False)
                raw_path = input_.get(path_key)
                if raw_path:
                    candidate = Path(raw_path)
                    if not candidate.is_absolute():
                        candidate = project / candidate
                    if not candidate.resolve().is_relative_to(project):
                        return PermissionResultDeny(message="Path is outside eval workspace", interrupt=False)
                if tool == "Glob":
                    pattern = Path(str(input_.get("pattern", "")))
                    if pattern.is_absolute() or ".." in pattern.parts:
                        return PermissionResultDeny(message="Glob escapes eval workspace", interrupt=False)
                return PermissionResultAllow(updated_input=input_)

            options = ClaudeAgentOptions(
                cwd=project,
                cli_path=shutil.which("claude"),
                model="haiku",
                max_turns=PER_QUERY_TURNS,
                max_budget_usd=PER_QUERY_BUDGET_USD,
                tools=["Read", "Write", "Edit", "Glob", "Grep"],
                permission_mode="default",
                can_use_tool=guard,
                setting_sources=[],
                settings=str(sdk_settings),
                extra_args={"no-chrome": None},
            )
            usage = WorkflowUsage()
            async with ClaudeSDKClient(options=options) as client:
                listing, _, _, tool_uses, successful_tool_ids = await ask(
                    client,
                    "Use the Glob tool with pattern * and path . to list files only in the "
                    "current working directory. Do not inspect parents or modify anything.",
                    usage,
                )
                require_tool(tool_uses, successful_tool_ids, "Glob", project, ".")
                if not any(
                    call.name == "Glob" and call.input.get("pattern") == "*"
                    for call in tool_uses
                ):
                    raise RuntimeError("listing turn did not use the required Glob pattern")
                if "initial.txt" not in listing:
                    raise RuntimeError("Claude did not report the isolated workspace sentinel")

                temp_file = project / "temp.md"
                _, _, _, tool_uses, successful_tool_ids = await ask(
                    client,
                    f"Use the Write tool with file_path {temp_file} to create it with exactly "
                    "this one line: alpha-marker-7319",
                    usage,
                )
                require_tool(tool_uses, successful_tool_ids, "Write", project, "temp.md")
                if not (project / "temp.md").exists() or (project / "temp.md").read_text().splitlines() != ["alpha-marker-7319"]:
                    raise RuntimeError("create step did not produce the exact one-line temp.md")
                _, _, _, tool_uses, successful_tool_ids = await ask(
                    client,
                    f"Use the Edit tool on file_path {temp_file} to append a second line exactly: "
                    "beta-content-2846. Preserve the first line.",
                    usage,
                )
                require_tool(tool_uses, successful_tool_ids, "Edit", project, "temp.md")
                expected = ["alpha-marker-7319", "beta-content-2846"]
                if (project / "temp.md").read_text().splitlines() != expected:
                    raise RuntimeError("append step did not produce the exact two-line temp.md")
                checked, _, _, tool_uses, successful_tool_ids = await ask(
                    client,
                    f"Use the Read tool with file_path {temp_file} and report both lines exactly.",
                    usage,
                )
                require_tool(tool_uses, successful_tool_ids, "Read", project, "temp.md")
                if not all(marker in checked for marker in expected):
                    raise RuntimeError("Claude did not report both temp.md markers")
                await inspect_in_chromium(project / "temp.md", text=expected[-1])
                print("files/create/check/browser: PASS", flush=True)

                await client.set_model("sonnet")
                model_reply, models, result, tool_uses, _ = await ask(
                    client,
                    "Reply with exactly model-switch-ok. Do not use tools or modify files.",
                    usage,
                )
                if tool_uses:
                    raise RuntimeError("model verification turn unexpectedly used tools")
                used_models = models | set((result.model_usage or {}).keys())
                if "model-switch-ok" not in model_reply.lower() or not any("sonnet" in model.lower() for model in used_models):
                    raise RuntimeError(f"post-model-switch turn was not Sonnet: {sorted(used_models)}")
                print("acknowledged SDK model switch: PASS", flush=True)

                game = project / "index.html"
                _, _, _, tool_uses, successful_tool_ids = await ask(
                    client,
                    f"Use the Write tool with file_path {game} to create a polished "
                    "self-contained browser game. It must have a Start button with id start, a "
                    "clickable target with id target, and a visible score element with id score. "
                    "Start begins the game and clicking the target increments score by exactly one. "
                    "Score must never change automatically. Use no external dependencies.",
                    usage,
                )
                require_tool(tool_uses, successful_tool_ids, "Write", project, "index.html")
                await inspect_in_chromium(game, game=True)
                print("game build/browser behavior: PASS", flush=True)

                review = project / "REVIEW.md"
                _, _, _, tool_uses, successful_tool_ids = await ask(
                    client,
                    f"Review {game} for correctness, accessibility, security, and "
                    "maintainability. Fix concrete issues, preserve the required IDs and behavior, "
                    "and leave the page with no browser console or page errors. Do not add CSP "
                    "directives that browsers reject in a meta element, such as frame-ancestors; "
                    f"omit unsupported header-only protections from the file. Write {review} with "
                    "Accessibility, Security, Correctness, and Maintainability sections.",
                    usage,
                )
                require_tool(tool_uses, successful_tool_ids, "Read", project, "index.html")
                require_tool(tool_uses, successful_tool_ids, "Write", project, "REVIEW.md")
                review_text = review.read_text() if review.exists() else ""
                require_review_sections(review_text)
                await inspect_in_chromium(game, game=True)
                print("game review/post-review browser behavior: PASS", flush=True)
                print(
                    f"workflow usage: ${usage.cost_usd:.4f}, {usage.turns} turns",
                    flush=True,
                )
    finally:
        model_after = current_global_model(global_settings)
        if model_after != model_before:
            raise RuntimeError("Claude SDK workflow changed the global model setting")

    print("REAL-CLAUDE SDK WORKFLOW: PASS", flush=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(asyncio.wait_for(main(), timeout=1200)))
