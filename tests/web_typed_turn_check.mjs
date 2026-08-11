await import("../converse_code/web/typed-turn.js");

const sent = [];
const busy = [];
const errors = [];
let clears = 0;
let timerCallback = null;
const controller = new globalThis.TypedTurnController({
  send: async (text) => { sent.push(text); },
  setBusy: (value) => { busy.push(value); },
  clearInput: () => { clears += 1; },
  showError: (detail) => { errors.push(detail); },
}, {
  setTimer: (callback) => { timerCallback = callback; return 1; },
  clearTimer: () => {},
});

if (!await controller.submit(" first ")) throw new Error("first typed turn was rejected");
if (await controller.submit("second")) throw new Error("concurrent typed turn was accepted");
if (sent.join(",") !== "first") throw new Error(`unexpected sends: ${sent}`);
if (controller.handleAsr("voice transcript")) throw new Error("voice ASR acknowledged typed turn");
if (!controller.handleAsr("first")) throw new Error("canonical ASR did not acknowledge typed turn");
if (clears !== 1 || busy.join(",") !== "true,false") {
  throw new Error("acknowledgement did not clear and unlock the composer");
}

await controller.submit("retry me");
timerCallback();
if (!errors.at(-1).includes("not acknowledged")) throw new Error("timeout was not surfaced");
if (clears !== 1 || busy.at(-1) !== false) throw new Error("timeout destroyed input or left it busy");

const failed = new globalThis.TypedTurnController({
  send: async () => { throw new Error("offline"); },
  setBusy: () => {},
  clearInput: () => { throw new Error("failed sends must not clear input"); },
  showError: (detail) => { errors.push(detail); },
});
if (await failed.submit("keep me")) throw new Error("failed send reported success");
if (!errors.at(-1).includes("offline")) throw new Error("send failure was not surfaced");

let resolveDeferred;
let deferredTimer = null;
const deferred = new globalThis.TypedTurnController({
  send: () => new Promise((resolve) => { resolveDeferred = resolve; }),
  setBusy: () => {},
  clearInput: () => {},
  showError: (detail) => { errors.push(detail); },
}, {
  setTimer: (callback) => { deferredTimer = callback; return 2; },
  clearTimer: () => {},
});
const inFlight = deferred.submit("slow connection");
await Promise.resolve();
if (deferredTimer !== null) throw new Error("ack timer started before send completed");
if (await deferred.submit("must serialize")) throw new Error("send-in-flight was not serialized");
resolveDeferred();
await inFlight;
if (!deferredTimer) throw new Error("ack timer did not start after send completed");
let resolveSession;
let currentVoiceRequest = true;
let micStarts = 0;
const delayedClient = {
  startMic: async () => { micStarts += 1; },
  stopMic: async () => {},
};
const beforeCapture = globalThis.startVoiceCapture(
  () => new Promise((resolve) => { resolveSession = resolve; }),
  () => currentVoiceRequest,
  {},
);
await Promise.resolve();
currentVoiceRequest = false;
resolveSession(delayedClient);
if (!(await beforeCapture).canceled || micStarts !== 0) {
  throw new Error("canceled connection started microphone capture");
}

let resolveCapture;
let micStops = 0;
currentVoiceRequest = true;
const acquiringClient = {
  startMic: () => new Promise((resolve) => { resolveCapture = resolve; }),
  stopMic: async () => { micStops += 1; },
};
const duringCapture = globalThis.startVoiceCapture(
  async () => acquiringClient, () => currentVoiceRequest, {},
);
while (!resolveCapture) await Promise.resolve();
currentVoiceRequest = false;
resolveCapture();
if (!(await duringCapture).canceled || micStops !== 1) {
  throw new Error("capture-acquisition race did not release microphone");
}
const rows = [];
const userTurns = new globalThis.UserTurnRenderer({
  create: () => { const row = {text: ""}; rows.push(row); return row; },
  setText: (row, text) => { row.text = text; },
});
userTurns.handle({turn_id: "voice-1"}, "voice partial", false);
userTurns.handle({turn_id: "typed-1"}, "typed echo", true);
userTurns.handle({turn_id: "voice-1"}, "voice final", true);
if (rows.length !== 2 || rows[0].text !== "voice final" || rows[1].text !== "typed echo") {
  throw new Error("interleaved voice and typed turns corrupted transcript rows");
}
userTurns.handle({turn_id: "typed-1"}, "typed correction", true);
if (rows.length !== 2 || rows[1].text !== "typed correction") {
  throw new Error("canonical correction did not update its stable turn row");
}
userTurns.handle({}, "legacy final", true);
userTurns.handle({}, "next legacy final", true);
if (rows.length !== 4) throw new Error("unkeyed final turns were merged");

console.log("typed turn lifecycle: OK");
