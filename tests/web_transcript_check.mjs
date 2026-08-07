/* The transcript must revise an interrupted turn in place, while preserving a
   genuinely new assistant turn as a separate row. */
await import("../converse_code/web/assistant-transcript.js");

const rows = [];
const transcript = new globalThis.AssistantTranscript({
  create: () => {
    const row = { text: "" };
    rows.push(row);
    return row;
  },
  setText: (row, text) => { row.text = text; },
  pickText: (event) => event.text || "",
});

transcript.handle({ type: "turn", turn_id: "turn-1" });
transcript.handle({ type: "text_delta", turn_id: "turn-1", delta: "It's a lovely day to be chatting." });
transcript.handle({ type: "interrupted", turn_id: "turn-1", barge_seq: 3 });
transcript.handle({ type: "utterance", turn_id: "turn-1", text: "It's a lovely day to be chattin [interrupted]" });

if (rows.length !== 1) {
  throw new Error(`corrected interrupted turn created ${rows.length} rows instead of replacing one`);
}
if (rows[0].text !== "It's a lovely day to be chattin [interrupted]") {
  throw new Error(`corrected interrupted text was not applied: ${rows[0].text}`);
}

transcript.handle({ type: "turn", turn_id: "turn-2" });
transcript.handle({ type: "utterance", turn_id: "turn-2", text: "A genuinely new reply." });

if (rows.length !== 2 || rows[1].text !== "A genuinely new reply.") {
  throw new Error("a genuinely new assistant turn did not get its own row");
}

transcript.reset();
transcript.handle({ type: "turn", turn_id: "turn-1" });
transcript.handle({ type: "utterance", turn_id: "turn-1", text: "Same id, new session." });
if (rows.length !== 3 || rows[2].text !== "Same id, new session.") {
  throw new Error("a new session reused an old transcript row");
}

console.log("assistant transcript revisions: OK");
