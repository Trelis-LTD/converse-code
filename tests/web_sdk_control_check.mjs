import assert from "node:assert/strict";

import { ConverseClient } from "../converse_code/web/vendor/converse/index.js";

class UnusedWebSocket {}

const client = new ConverseClient({
  url: "wss://example.invalid/ws",
  player: {},
  WebSocketImpl: UnusedWebSocket,
});

const calls = [
  () => client.sendToolResult("call", {}, {}),
  () => client.sendToolDeferred("call", {handle: "job"}),
  () => client.sendToolProgress("call", "working"),
  () => client.sendToolPartialResult("call", {}),
  () => client.sendToolCancel("call"),
];

for (const call of calls) assert.equal(call(), false);

const sent = [];
client.ws = {readyState: 1, send: (frame) => sent.push(JSON.parse(frame))};
for (const call of calls) assert.equal(call(), true);
assert.equal(sent.length, calls.length);
console.log("SDK tool controls expose transport delivery: OK");
