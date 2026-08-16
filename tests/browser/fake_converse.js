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
    this.emit({type: "warming_up", attempt: 1, device_id: null});
    if (state.startMicError) {
      const error = new Error(state.startMicError);
      this.emit({type: "failed", code: "capture_failed", error});
      throw error;
    }
    this.stream = await navigator.mediaDevices.getUserMedia({audio: true});
    this.emit({type: "listening", device_id: "default", recovered: false});
  }

  stopMic() {
    if (this.stream) this.stream.getTracks().forEach((track) => track.stop());
    this.stream = null;
  }

  setMicEnabled(enabled) {
    this.micEnabled = enabled;
  }

  sendToolResult(id, content, options) {
    if (state.transportLive) this.bridgeCalls.push({action: "tool_result", id, content, options});
    return state.transportLive;
  }
  sendToolDeferred(id, options) {
    if (state.transportLive) this.bridgeCalls.push({action: "tool_deferred", id, options});
    return state.transportLive;
  }
  sendToolPartialResult(id, content, options) {
    if (state.transportLive) {
      this.bridgeCalls.push({action: "tool_partial_result", id, content, options});
    }
    return state.transportLive;
  }
  sendToolCancel(id) {
    if (state.transportLive) this.bridgeCalls.push({action: "tool_cancel", id});
    return state.transportLive;
  }

  close() { this.closed = true; this.stopMic(); }
  async closeAndWait() { this.close(); }

  emit(detail) {
    this.dispatchEvent(new CustomEvent("event", {detail}));
  }
}
