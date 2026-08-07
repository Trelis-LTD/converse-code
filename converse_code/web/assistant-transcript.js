(function(root){
  "use strict";

  class AssistantTranscript {
    constructor(view){
      this.view = view;
      this.entries = new Map();
      this.currentTurnId = null;
      this.anonymousSequence = 0;
    }

    _turnId(event, create){
      if(event && event.turn_id) return String(event.turn_id);
      if(this.currentTurnId) return this.currentTurnId;
      if(!create) return null;
      this.anonymousSequence += 1;
      return "anonymous-" + this.anonymousSequence;
    }

    _entry(turnId){
      var state = this.entries.get(turnId);
      if(!state){
        state = { element: this.view.create(), text: "" };
        this.entries.set(turnId, state);
      }
      return state;
    }

    reset(){
      this.entries.clear();
      this.currentTurnId = null;
    }

    handle(event){
      switch(event.type){
        case "turn": {
          var startedId = this._turnId(event, true);
          this.currentTurnId = startedId;
          this._entry(startedId);
          break;
        }
        case "text_delta": {
          var deltaId = this._turnId(event, true);
          this.currentTurnId = deltaId;
          var deltaState = this._entry(deltaId);
          deltaState.text += event.delta || "";
          this.view.setText(deltaState.element, deltaState.text);
          break;
        }
        case "utterance": {
          var utteranceId = this._turnId(event, true);
          var utteranceState = this._entry(utteranceId);
          utteranceState.text = this.view.pickText(event);
          this.view.setText(utteranceState.element, utteranceState.text);
          break;
        }
        case "done":
        case "interrupted": {
          var finishedId = this._turnId(event, false);
          if(!finishedId || finishedId === this.currentTurnId) this.currentTurnId = null;
          break;
        }
        default:
          break;
      }
    }
  }

  root.AssistantTranscript = AssistantTranscript;
})(typeof globalThis !== "undefined" ? globalThis : window);
