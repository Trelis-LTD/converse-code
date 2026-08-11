import bridgeExtension from "../converse_code/pi_bridge.ts";
import approvalExtension from "../converse_code/pi_approval.ts";

class FakeSocket {
  static OPEN = 1;
  static latest = null;
  constructor(url) {
    this.url = url;
    this.readyState = FakeSocket.OPEN;
    this.listeners = new Map();
    this.frames = [];
    FakeSocket.latest = this;
  }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  emit(name, value = {}) { this.listeners.get(name)?.(value); }
  send(value) { this.frames.push(JSON.parse(value)); }
  close() { this.readyState = 3; }
}

globalThis.WebSocket = FakeSocket;
process.env.CONVERSE_CODE_PI_BRIDGE_URL = "ws://127.0.0.1:1234/pi?t=test";

function fakePi() {
  const handlers = new Map();
  return {
    handlers,
    userMessages: [],
    on(name, callback) { handlers.set(name, callback); },
    sendUserMessage(message, options) {
      this.userMessages.push({message, options});
      handlers.get("input")?.({text: message, source: "extension", streamingBehavior: options?.deliverAs});
    },
  };
}

const pi = fakePi();
let idle = true;
const statuses = [];
const context = {
  model: {id: "gpt-test"},
  isIdle: () => idle,
  abort: () => { context.aborted = true; },
  shutdown: () => { context.shutDown = true; },
  ui: {setStatus: (_id, text) => statuses.push(text)},
};
bridgeExtension(pi);
pi.handlers.get("session_start")({}, context);
const socket = FakeSocket.latest;
socket.emit("open");
socket.emit("message", {data: JSON.stringify({id: "p1", type: "prompt", message: "Fix it"})});
idle = false;
socket.emit("message", {data: JSON.stringify({id: "s1", type: "steer", message: "Also test"})});
pi.handlers.get("agent_start")({type: "agent_start"});
pi.handlers.get("tool_execution_start")({
  type: "tool_execution_start", toolCallId: "t1", toolName: "edit", args: {path: "app.py"},
});
pi.handlers.get("message_end")({type: "message_end", message: {role: "assistant", content: "Done"}});
pi.handlers.get("agent_settled")({type: "agent_settled"});
socket.emit("message", {data: JSON.stringify({id: "x1", type: "shutdown"})});
await new Promise((resolve) => setTimeout(resolve, 1));

if (pi.userMessages.length !== 2) throw new Error("bridge did not inject both user messages");
if (pi.userMessages[0].message !== "Fix it" || pi.userMessages[0].options !== undefined) {
  throw new Error("idle prompt was not injected as a normal visible-TUI user turn");
}
if (pi.userMessages[1].options?.deliverAs !== "steer") throw new Error("steer was not semantic");
const types = socket.frames.map((frame) => frame.type);
for (const expected of ["bridge_ready", "response", "agent_start", "tool_execution_start", "message_end", "agent_settled"]) {
  if (!types.includes(expected)) throw new Error(`missing extension frame: ${expected}`);
}
const ownedInputs = socket.frames.filter((frame) => frame.type === "input_seen");
if (ownedInputs.length !== 2 || ownedInputs.some((frame) => frame.owner !== "bridge")) {
  throw new Error("bridge inputs were not explicitly owned");
}
if (ownedInputs[0].commandId !== "p1" || ownedInputs[1].commandId !== "s1") {
  throw new Error("bridge input ownership was not command-correlated");
}
if (!statuses.includes("Converse voice: connected")) throw new Error("visible status was not set");
if (context.shutDown !== true) throw new Error("shutdown was not semantic");

const approvalPi = fakePi();
approvalExtension(approvalPi);
let requested = null;
const approvalContext = {ui: {select: async (title, options) => {
  requested = {title, options};
  return "Block";
}}};
const decision = await approvalPi.handlers.get("tool_call")({
  toolName: "bash", input: {command: "uv run pytest -q"},
}, approvalContext);
if (!requested.title.includes("uv run pytest -q")) throw new Error("approval omitted command");
if (requested.options.join("|") !== "Allow once|Allow for session|Block") {
  throw new Error("approval options changed");
}
if (decision?.block !== true) throw new Error("blocked approval did not block the tool");

console.log("Pi extension contract: passed");
