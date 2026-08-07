import json

from converse_code.hooks import write_settings


def test_settings_register_completion_and_prompt_submission_hooks(tmp_path):
    path = write_settings(
        tmp_path,
        "http://127.0.0.1/hook/stop",
        "http://127.0.0.1/hook/user_prompt_submit",
        "http://127.0.0.1/hook/permission_request",
    )

    settings = json.loads(path.read_text())
    hooks = settings["hooks"]
    assert set(hooks) == {"Stop", "UserPromptSubmit", "PermissionRequest"}
    assert hooks["Stop"][0]["hooks"][0] == {
        "type": "http",
        "url": "http://127.0.0.1/hook/stop",
        "timeout": 5,
    }
    assert hooks["UserPromptSubmit"][0]["hooks"][0] == {
        "type": "http",
        "url": "http://127.0.0.1/hook/user_prompt_submit",
        "timeout": 5,
    }
    assert hooks["PermissionRequest"][0]["hooks"][0] == {
        "type": "http",
        "url": "http://127.0.0.1/hook/permission_request",
        "timeout": 5,
    }
