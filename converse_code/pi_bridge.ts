// Semantic localhost bridge for a normal, visible Pi TUI. This uses Pi's extension API only:
// no terminal scraping, keystroke injection, or hidden RPC process.
export default function (pi) {
  const url = process.env.CONVERSE_CODE_PI_BRIDGE_URL;
  let socket = null;
  let context = null;
  let reconnectTimer = null;
  let shuttingDown = false;
  let allowForSession = false;
  let nextApprovalId = 1;
  const expectedInputs = [];
  const pendingApprovals = new Map();

  const send = (frame) => {
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(frame));
  };

  const setStatus = (text) => {
    if (context) context.ui.setStatus("converse-code", text);
  };

  const respond = (id, success, error) => {
    send({type: "response", id, success, ...(error ? {error: String(error)} : {})});
  };

  const injectUserMessage = (id, message, options) => {
    const expected = {id, message};
    expectedInputs.push(expected);
    try {
      pi.sendUserMessage(message, options);
    } catch (error) {
      const index = expectedInputs.indexOf(expected);
      if (index >= 0) expectedInputs.splice(index, 1);
      throw error;
    }
  };

  const resolvePendingApprovals = (decision, reason) => {
    for (const [id, pending] of pendingApprovals) {
      clearTimeout(pending.timer);
      pending.resolve({decision, ...(reason ? {reason} : {})});
      pendingApprovals.delete(id);
    }
  };
  const failPendingApprovals = (reason) => resolvePendingApprovals("block", reason);

  const handleCommand = (frame) => {
    if (!context || typeof frame?.id !== "string") return;
    try {
      const message = String(frame.message || "").trim();
      if (frame.type === "prompt") {
        if (!message) throw new Error("a prompt is required");
        if (!context.isIdle()) throw new Error("Pi is already working; steer it instead");
        injectUserMessage(frame.id, message);
      } else if (frame.type === "steer") {
        if (!message) throw new Error("a message is required");
        if (context.isIdle()) throw new Error("Pi is no longer working on an active turn");
        injectUserMessage(frame.id, message, {deliverAs: "steer"});
      } else if (frame.type === "abort") {
        failPendingApprovals("the coding task was cancelled");
        context.abort();
      } else if (frame.type === "approval_response") {
        const pending = pendingApprovals.get(String(frame.approvalId || ""));
        if (!pending) throw new Error("approval is no longer pending");
        if (!["allow_once", "allow_session", "block"].includes(frame.decision)) {
          throw new Error("invalid approval decision");
        }
        pendingApprovals.delete(frame.approvalId);
        clearTimeout(pending.timer);
        if (frame.decision === "allow_session") {
          allowForSession = true;
          resolvePendingApprovals("allow_session");
        }
        pending.resolve({decision: frame.decision});
      } else if (frame.type === "shutdown") {
        respond(frame.id, true);
        setTimeout(() => context.shutdown(), 0);
        return;
      } else {
        throw new Error(`unsupported bridge command: ${frame.type}`);
      }
      respond(frame.id, true);
    } catch (error) {
      respond(frame.id, false, error?.message || error);
    }
  };

  const connect = () => {
    if (!url || shuttingDown) return;
    setStatus("Converse voice: connecting");
    socket = new WebSocket(url);
    socket.addEventListener("open", () => {
      setStatus("Converse voice: connected");
      send({type: "bridge_ready"});
    });
    socket.addEventListener("message", (event) => {
      try { handleCommand(JSON.parse(String(event.data))); }
      catch (error) { send({type: "extension_error", error: String(error)}); }
    });
    socket.addEventListener("close", () => {
      failPendingApprovals("the Converse approval bridge disconnected");
      socket = null;
      if (!shuttingDown) {
        setStatus("Converse voice: reconnecting");
        reconnectTimer = setTimeout(connect, 500);
      }
    });
    socket.addEventListener("error", () => socket?.close());
  };

  pi.on("session_start", (_event, ctx) => {
    context = ctx;
    connect();
  });
  pi.on("input", (event) => {
    const expected = expectedInputs[0];
    if (event.source === "extension" && expected && event.text === expected.message) {
      expectedInputs.shift();
      send({
        type: "input_seen",
        owner: "bridge",
        commandId: expected.id,
      });
      return;
    }
    expectedInputs.length = 0;
    failPendingApprovals("unrelated terminal or extension input interrupted approval");
    send({
      type: "input_seen",
      owner: event.source === "interactive" ? "interactive" : "other",
    });
  });
  pi.on("agent_start", (event) => send({type: event.type}));
  pi.on("tool_execution_start", (event) => send({
    type: event.type,
    toolCallId: event.toolCallId,
    toolName: event.toolName,
    args: event.args,
  }));
  pi.on("tool_call", async (event) => {
    if (allowForSession || !["bash", "edit", "write"].includes(event.toolName)) return;
    if (socket?.readyState !== WebSocket.OPEN) {
      return {block: true, reason: "Blocked because Converse approval is disconnected."};
    }
    const input = event.input || {};
    const target = event.toolName === "bash"
      ? String(input.command || "")
      : String(input.path || input.file_path || "project file");
    const summary = target.replace(/\s+/g, " ").slice(0, 180);
    const approvalId = `approval-${nextApprovalId++}`;
    setStatus(`Waiting for approval: ${event.toolName}`);
    const result = await new Promise((resolve) => {
      const timer = setTimeout(() => {
        pendingApprovals.delete(approvalId);
        resolve({decision: "block", reason: "approval timed out"});
      }, 300000);
      pendingApprovals.set(approvalId, {resolve, timer});
      send({type: "approval_request", approvalId, toolName: event.toolName, summary});
    });
    setStatus("Converse voice: connected");
    if (result.decision === "block") {
      const reason = result.reason
        ? `Blocked because ${result.reason}: ${target}`
        : `Blocked by the user: ${target}`;
      return {block: true, reason};
    }
  });
  pi.on("message_end", (event) => send({type: event.type, message: event.message}));
  pi.on("agent_settled", (event) => {
    send({type: event.type});
    expectedInputs.length = 0;
  });
  pi.on("session_shutdown", () => {
    shuttingDown = true;
    expectedInputs.length = 0;
    failPendingApprovals("the Pi session ended");
    if (reconnectTimer) clearTimeout(reconnectTimer);
    send({type: "session_shutdown"});
    socket?.close();
  });
}
