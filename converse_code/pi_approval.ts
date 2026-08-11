// Minimal structured approval gate for Pi RPC. In RPC mode ctx.ui.select is transported as an
// extension_ui_request, so Converse can ask the user and return an ID-correlated answer.
export default function (pi) {
  let allowForSession = false;
  pi.on("tool_call", async (event, ctx) => {
    if (allowForSession || !["bash", "edit", "write"].includes(event.toolName)) return;
    const input = event.input || {};
    const target = event.toolName === "bash"
      ? String(input.command || "").slice(0, 500)
      : String(input.path || input.file_path || "project file");
    const summary = target.replace(/\s+/g, " ").slice(0, 180);
    const choice = await ctx.ui.select(
      `Allow ${event.toolName}: ${summary}?`,
      ["Allow once", "Allow for session", "Block"],
    );
    if (choice === "Allow for session") allowForSession = true;
    if (!choice || choice === "Block") {
      return {block: true, reason: `Blocked by the user through Converse: ${target}`};
    }
  });
}
