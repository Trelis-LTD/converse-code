const state = globalThis.__fakeConverse = Object.assign({
  clients: [],
  transportLive: true,
  retryInjectionOnce: false,
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
    this.injections = [];
    this.stream = null;
    state.clients.push(this);
  }

  async unlockAudio() {}

  async connect() {}

  async injectContext(text, options) {
    this.injections.push({text, options});
    if (state.retryInjectionOnce) {
      state.retryInjectionOnce = false;
      const turnId = `raced-${options.messageId}`;
      this.emit({type: "turn", turn_id: turnId});
      setTimeout(() => this.emit({type: "done", turn_id: turnId}), 10);
      return {accepted: false, retryable: true, message_id: options.messageId};
    }
    if (options.reply) setTimeout(() => {
      const turnId = `injection-${options.messageId}`;
      this.emit({type: "turn", turn_id: turnId});
      this.emit({type: "text_delta", delta: "Would you like to allow that action once, for this session, or block it?", turn_id: turnId});
      this.emit({type: "done", turn_id: turnId});
    }, 0);
    return {accepted: true, message_id: options.messageId};
  }

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
