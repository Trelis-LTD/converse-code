const state = globalThis.__fakeConverse = Object.assign({
  clients: [],
  connectOptions: [],
  micStarts: 0,
  micStops: 0,
  micEnabled: [],
  autoReply: true,
  deferConnect: false,
  resolveConnect: null,
  deferMic: false,
  resolveMic: null,
}, globalThis.__fakeConverse || {});

export function createSessionId() {
  return "browser-e2e-session";
}

export class ConverseClient extends EventTarget {
  constructor(options) {
    super();
    this.options = options;
    this.sessionId = options.sessionId;
    this.injected = [];
    this.bridgeCalls = [];
    this.stream = null;
    state.clients.push(this);
  }

  async unlockAudio() {}

  async connect(options = {}) {
    state.connectOptions.push(options);
    if (state.deferConnect) {
      await new Promise((resolve) => { state.resolveConnect = resolve; });
      state.deferConnect = false;
      state.resolveConnect = null;
    }
  }

  async startMic() {
    state.micStarts += 1;
    this.stream = await navigator.mediaDevices.getUserMedia({audio: true});
    if (state.deferMic) {
      await new Promise((resolve) => { state.resolveMic = resolve; });
      state.deferMic = false;
      state.resolveMic = null;
    }
  }

  stopMic() {
    state.micStops += 1;
    if (this.stream) this.stream.getTracks().forEach((track) => track.stop());
    this.stream = null;
  }

  setMicEnabled(enabled) {
    state.micEnabled.push(enabled);
    if (this.stream) this.stream.getAudioTracks().forEach((track) => { track.enabled = enabled; });
  }

  injectContext(text, options) {
    this.injected.push({text, options});
    const turnId = `typed-${this.injected.length}`;
    if (!state.autoReply) return;
    setTimeout(() => {
      this.emit({type: "asr", text, final: true, turn_id: turnId});
      this.emit({type: "turn", turn_id: turnId});
      this.emit({type: "text_delta", delta: `Acknowledged: ${text}`, turn_id: turnId});
      this.emit({type: "done", turn_id: turnId});
    }, 10);
  }

  sendToolResult(id, content) { this.bridgeCalls.push({action: "tool_result", id, content}); }
  sendToolProgress(id, note) { this.bridgeCalls.push({action: "tool_progress", id, note}); }
  sendToolDeferred(id, options) { this.bridgeCalls.push({action: "tool_deferred", id, options}); }
  sendToolPartialResult(id, content, options) {
    this.bridgeCalls.push({action: "tool_partial_result", id, content, options});
  }
  sendToolCancel(id) { this.bridgeCalls.push({action: "tool_cancel", id}); }

  exportResumeState() { return {token: "browser-e2e-resume"}; }
  importResumeState(value) { this.resumeState = value; }
  close() { this.stopMic(); }

  emit(detail) {
    this.dispatchEvent(new CustomEvent("event", {detail}));
  }
}
