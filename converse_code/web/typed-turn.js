(function(root){
  "use strict";

  class TypedTurnController {
    constructor(view, options){
      options = options || {};
      this.view = view;
      this.timeoutMs = options.timeoutMs || 15000;
      this.setTimer = options.setTimer || ((callback, delay) => setTimeout(callback, delay));
      this.clearTimer = options.clearTimer || ((timer) => clearTimeout(timer));
      this.pending = null;
    }

    async submit(rawText){
      var text = String(rawText || "").trim();
      if(!text || this.pending) return false;
      var pending = {text: text, timer: null};
      this.pending = pending;
      this.view.setBusy(true);
      try{
        await this.view.send(text);
        if(this.pending === pending){
          pending.timer = this.setTimer(() => {
            if(this.pending === pending){
              this.fail("Typed turn was not acknowledged. It remains in the input so you can retry.");
            }
          }, this.timeoutMs);
        }
        return true;
      }catch(error){
        this.fail("Could not send typed turn: " + (error && error.message ? error.message : error));
        return false;
      }
    }

    handleAsr(text){
      if(!this.pending || text !== this.pending.text) return false;
      this._settle(true);
      return true;
    }

    fail(detail){
      if(!this.pending) return;
      this._settle(false);
      this.view.showError(detail);
    }

    reset(){
      this._settle(false);
    }

    _settle(acknowledged){
      if(!this.pending) return;
      this.clearTimer(this.pending.timer);
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
