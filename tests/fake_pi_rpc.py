import json
import sys


exit_on_command = "--exit-on-command" in sys.argv
streaming = False

for line in sys.stdin:
    command = json.loads(line)
    if exit_on_command:
        raise SystemExit(3)
    kind = command["type"]
    request_id = command.get("id")
    if kind == "get_state":
        print(json.dumps({
            "id": request_id, "type": "response", "command": kind,
            "success": True, "data": {"isStreaming": streaming},
        }), flush=True)
    elif kind == "prompt":
        streaming = True
        print(json.dumps({
            "id": request_id, "type": "response", "command": kind, "success": True,
        }), flush=True)
        for event in (
            {"type": "agent_start"},
            {"type": "tool_execution_start", "toolCallId": "tool-1", "toolName": "bash",
             "args": {"command": "pytest -q"}},
            {"type": "message_end", "message": {
                "role": "assistant", "content": [{"type": "text", "text": "All tests pass."}],
            }},
            {"type": "agent_settled"},
        ):
            print(json.dumps(event), flush=True)
        streaming = False
    elif kind == "abort":
        print(json.dumps({
            "id": request_id, "type": "response", "command": kind,
            "success": False, "error": "not streaming",
        }), flush=True)
    else:
        print(json.dumps({
            "id": request_id, "type": "response", "command": kind, "success": True,
        }), flush=True)
