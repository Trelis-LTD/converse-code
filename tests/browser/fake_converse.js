const state = globalThis.__fakeConverse = Object.assign({
  clients: [],
  transportLive: true,
}, globalThis.__fakeConverse || {});

export function createSessionId() {
  return "browser-e2e-session";
}

export class ConverseClient extends EventTarget {
  constructor(options) {
    super();
    this.options = options;
    this.sessionId = options.sessionId;
    this.bridgeCalls = [];
    this.stream = null;
    state.clients.push(this);
  }

  async unlockAudio() {}

  async connect() {}

  async startMic() {
    this.stream = await navigator.mediaDevices.getUserMedia({audio: true});
  }

  stopMic() {
    if (this.stream) this.stream.getTracks().forEach((track) => track.stop());
    this.stream = null;
  }

  sendToolResult(id, content, options) {
    if (state.transportLive) this.bridgeCalls.push({action: "tool_result", id, content, options});
    return state.transportLive;
  }
  sendToolProgress(id, note) {
    if (state.transportLive) this.bridgeCalls.push({action: "tool_progress", id, note});
    return state.transportLive;
  }
  sendToolDeferred(id, options) {
    if (state.transportLive) this.bridgeCalls.push({action: "tool_deferred", id, options});
    return state.transportLive;
  }
  sendToolPartialResult(id, content, options) {
    if (state.transportLive) {
      this.bridgeCalls.push({action: "tool_partial_result", id, content, options});
      if (options.reply) setTimeout(() => {
        this.emit({type: "turn", turn_id: `partial-${id}`});
        this.emit({type: "text_delta", delta: content.speak, turn_id: `partial-${id}`});
        this.emit({type: "done", turn_id: `partial-${id}`});
      }, 0);
    }
    return state.transportLive;
  }
  sendToolCancel(id) {
    if (state.transportLive) this.bridgeCalls.push({action: "tool_cancel", id});
    return state.transportLive;
  }

  close() { this.stopMic(); }

  emit(detail) {
    this.dispatchEvent(new CustomEvent("event", {detail}));
  }
}
