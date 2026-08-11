await import("../converse_code/web/typed-turn.js");

const sent = [];
const busy = [];
const errors = [];
let clears = 0;
const controller = new globalThis.TypedTurnController({
  send: async (text, messageId) => { sent.push({text, messageId}); return {message_id: messageId, accepted: true}; },
  setBusy: (value) => { busy.push(value); },
  clearInput: () => { clears += 1; },
  showError: (detail) => { errors.push(detail); },
}, {messageId: () => "message-1"});

if (!await controller.submit(" first ")) throw new Error("first typed turn was rejected");
if (sent.length !== 1 || sent[0].text !== "first" || sent[0].messageId !== "message-1") {
  throw new Error(`unexpected send: ${JSON.stringify(sent)}`);
}
if (controller.handleAsr({input_source: "voice", message_id: "message-1"}, "first")) {
  throw new Error("voice ASR acknowledged typed turn");
}
if (!controller.handleAsr({input_source: "text", message_id: "message-1"}, "first")) {
  throw new Error("canonical correlated ASR did not confirm typed turn");
}
if (clears !== 1 || busy.join(",") !== "true,false") {
  throw new Error("acknowledgement did not clear and unlock the composer");
}

const failed = new globalThis.TypedTurnController({
  send: async () => { throw new Error("offline"); },
  setBusy: () => {},
  clearInput: () => { throw new Error("failed sends must not clear input"); },
  showError: (detail) => { errors.push(detail); },
});
if (await failed.submit("keep me")) throw new Error("failed send reported success");
if (!errors.at(-1).includes("offline")) throw new Error("send failure was not surfaced");

let resolveDeferred;
const deferred = new globalThis.TypedTurnController({
  send: (_text, messageId) => new Promise((resolve) => { resolveDeferred = () => resolve({
    message_id: messageId, accepted: true,
  }); }),
  setBusy: () => {},
  clearInput: () => {},
  showError: (detail) => { errors.push(detail); },
}, {messageId: () => "slow-1"});
const inFlight = deferred.submit("slow connection");
await Promise.resolve();
if (await deferred.submit("must serialize")) throw new Error("ack-in-flight turn was not serialized");
resolveDeferred();
if (!await inFlight) throw new Error("accepted deferred acknowledgement failed");

const rejected = new globalThis.TypedTurnController({
  send: async (_text, messageId) => ({message_id: messageId, accepted: false, reason: "busy"}),
  setBusy: () => {},
  clearInput: () => { throw new Error("rejected sends must not clear input"); },
  showError: (detail) => { errors.push(detail); },
}, {messageId: () => "rejected-1"});
if (await rejected.submit("keep rejected")) throw new Error("rejected send reported success");
if (!errors.at(-1).includes("busy")) throw new Error("broker rejection was not surfaced");
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
