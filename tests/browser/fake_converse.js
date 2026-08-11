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
  deferTextAck: false,
  resolveTextAck: null,
  deferBridgeAck: false,
  resolveBridgeAck: null,
  rejectBridgeAck: null,
  connectErrors: [],
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
    this.injected = [];
    this.bridgeCalls = [];
    this.stream = null;
    state.clients.push(this);
  }

  async unlockAudio() {}

  async connect(options = {}) {
    state.connectOptions.push(options);
    if (state.connectErrors.length) {
      const spec = state.connectErrors.shift();
      const detail = {type: "error", code: spec.code, detail: spec.detail,
        retryable: spec.retryable};
      this.emit(detail);
      const error = new Error(spec.detail);
      error.code = spec.code;
      error.retryable = spec.retryable;
      throw error;
    }
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

  sendText(text, options = {}) {
    this.injected.push({text, options});
    const turnId = `typed-${this.injected.length}`;
    const acknowledgement = {
      type: "inject_context_ack", message_id: options.messageId,
      accepted: true, input_source: "text", turn_id: turnId,
    };
    const reply = () => {
      if (state.autoReply) setTimeout(() => {
        this.emit({type: "asr", text, final: true, turn_id: turnId,
          message_id: options.messageId, input_source: "text"});
        this.emit({type: "turn", turn_id: turnId});
        this.emit({type: "text_delta", delta: `Acknowledged: ${text}`, turn_id: turnId});
        this.emit({type: "done", turn_id: turnId});
      }, 10);
      return acknowledgement;
    };
    if (!state.deferTextAck) return Promise.resolve(reply());
    return new Promise((resolve) => { state.resolveTextAck = () => {
      state.deferTextAck = false;
      state.resolveTextAck = null;
      resolve(reply());
    }; });
  }

  injectContext(text, options = {}) {
    this.bridgeCalls.push({action: "inject_context", text, options});
    const acknowledgement = {
      type: "inject_context_ack", message_id: options.messageId,
      accepted: true, input_source: options.role === "user" ? "text" : "context",
    };
    if (!state.deferBridgeAck) return Promise.resolve(acknowledgement);
    return new Promise((resolve, reject) => {
      const finish = () => {
        state.deferBridgeAck = false;
        state.resolveBridgeAck = null;
        state.rejectBridgeAck = null;
      };
      state.resolveBridgeAck = () => {
        finish();
        resolve(acknowledgement);
      };
      state.rejectBridgeAck = () => {
        finish();
        reject(new Error("simulated injection transport loss"));
      };
    });
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
    }
    return state.transportLive;
  }
  sendToolCancel(id) {
    if (state.transportLive) this.bridgeCalls.push({action: "tool_cancel", id});
    return state.transportLive;
  }

  exportResumeState() { return {token: "browser-e2e-resume"}; }
  importResumeState(value) { this.resumeState = value; }
  close() { this.stopMic(); }

  emit(detail) {
    this.dispatchEvent(new CustomEvent("event", {detail}));
  }
}
