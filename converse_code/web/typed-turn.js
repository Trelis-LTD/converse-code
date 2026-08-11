(function(root){
  "use strict";

  class TypedTurnController {
    constructor(view, options){
      options = options || {};
      this.view = view;
      this.messageId = options.messageId || (() => globalThis.crypto?.randomUUID?.()
        || `typed-${Date.now()}-${Math.random().toString(36).slice(2)}`);
      this.pending = null;
      this.expectedEchoes = new Map();
    }

    async submit(rawText){
      var text = String(rawText || "").trim();
      if(!text || this.pending) return false;
      var pending = {text: text, messageId: this.messageId()};
      this.pending = pending;
      this.view.setBusy(true);
      try{
        var acknowledgement = await this.view.send(text, pending.messageId);
        if(this.pending !== pending) return false;
        if(!acknowledgement || acknowledgement.message_id !== pending.messageId){
          throw new Error("Converse returned an uncorrelated acknowledgement");
        }
        if(acknowledgement.accepted !== true){
          throw new Error(acknowledgement.reason || "Converse rejected the typed turn");
        }
        this.expectedEchoes.set(pending.messageId, text);
        this._settle(true);
        return true;
      }catch(error){
        this.fail("Could not send typed turn: " + (error && error.message ? error.message : error));
        return false;
      }
    }

    handleAsr(event, text){
      var messageId = event && (event.message_id !== undefined
        ? event.message_id : event.messageId);
      if(!event || event.input_source !== "text" || !messageId) return false;
      messageId = String(messageId);
      if(this.expectedEchoes.get(messageId) !== text) return false;
      this.expectedEchoes.delete(messageId);
      return true;
    }

    fail(detail){
      if(!this.pending) return;
      this._settle(false);
      this.view.showError(detail);
    }

    reset(){
      this._settle(false);
      this.expectedEchoes.clear();
    }

    _settle(acknowledged){
      if(!this.pending) return;
      this.pending = null;
      this.view.setBusy(false);
      if(acknowledged) this.view.clearInput();
    }
  }

  class UserTurnRenderer {
    constructor(view){
      this.view = view;
      this.byTurnId = new Map();
      this.fallback = null;
    }

    handle(event, text, final){
      var rawTurnId = event && (event.turn_id !== undefined ? event.turn_id : event.turnId);
      var turnId = rawTurnId === undefined || rawTurnId === null || rawTurnId === ""
        ? null : String(rawTurnId);
      var entry;
      if(turnId){
        entry = this.byTurnId.get(turnId);
        if(!entry){
          entry = this.view.create();
          this.byTurnId.set(turnId, entry);
        }
      }else{
        entry = this.fallback;
        if(!entry){
          entry = this.view.create();
          this.fallback = entry;
        }
      }
      this.view.setText(entry, text);
      if(final && !turnId) this.fallback = null;
      return entry;
    }

    freezeFallback(){
      this.fallback = null;
    }

    reset(){
      this.byTurnId.clear();
      this.fallback = null;
    }
  }

  async function startVoiceCapture(getClient, isCurrent, options){
    var client = await getClient();
    if(!isCurrent()) return {canceled: true};
    await client.startMic(options);
    if(!isCurrent()){
      try{ await client.stopMic(); }catch(error){}
      return {canceled: true};
    }
    return {canceled: false, client: client};
  }

  root.TypedTurnController = TypedTurnController;
  root.UserTurnRenderer = UserTurnRenderer;
  root.startVoiceCapture = startVoiceCapture;
})(typeof globalThis !== "undefined" ? globalThis : window);
