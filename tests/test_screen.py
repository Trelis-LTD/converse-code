from converse_code.screen import (
    Menu, ModelAcknowledgement, detect_current_model, detect_menu, detect_model,
    has_empty_composer, is_idle, is_model_scope_prompt, match_option,
    model_acknowledgements,
)

PERMISSION_PROMPT = [
    "  Claude wants to run: pytest tests/",
    "",
    "  Do you want to proceed?",
    "  ❯ 1. Yes",
    "    2. Yes, and don't ask again this session",
    "    3. No, and tell Claude what to do differently (esc)",
    "",
]

MODEL_PICKER = [
    " Select model:",
    "   Opus",
    " ❯ Sonnet",
    "   Haiku",
    "",
]

# Captured from the real Claude Code TUI: the input prompt is also a "❯",
# sitting between two horizontal rules — must never be treated as a menu.
REAL_IDLE_SCREEN = [
    "╭─── Claude Code v2.1.223 ─────────────────────╮",
    "│                 Welcome back!                │",
    "╰──────────────────────────────────────────────╯",
    "                            ◐ medium · /effort",
    "────────────────────────────────────────────────",
    "❯",
    "────────────────────────────────────────────────",
    "  ⏵⏵ auto mode on (shift+tab to cycle)",
]

# Captured after closing Claude Code 2.1.224's /model picker. Historical user
# prompts retain the same ❯ glyph as menu selections; only the bottom composer
# between rules is live, and none of these history rows is an open menu.
REAL_IDLE_WITH_PROMPT_HISTORY = [
    "❯ Reply with exactly the word pong and do nothing else.",
    "⏺ pong",
    "✻ Worked for 3s",
    "❯ /model",
    "  ⎿  Set model to Fable 5 and saved as your default for new sessions",
    "                                      ◐ medium · /effort",
    "────────────────────────────────────────────────────────",
    "❯ ",
    "────────────────────────────────────────────────────────",
    "  ⏵⏵ auto mode on (shift+tab to cycle)",
]

MODEL_SCOPE_PROMPT_2_1_227 = [
    "❯ /model",
    "  ⎿  Kept model as Haiku 4.5",
    "✻",
    "────────────────────────────────────────────────────────",
    "❯ T",
    "────────────────────────────────────────────────────────",
    "Enter to set as default · s to use this session only · Esc to cancel",
]


WELCOME_WITH_MODEL = [
    "│  Sonnet 5 with medium effort · Claude Max · Ronan  │",
    "│                 ~/TR/converse-code                  │",
]

# Numbered list in Claude's prose + the prompt cursor: not a menu either.
PROSE_WITH_NUMBERS = [
    " Here's the plan:",
    " 1. Refactor the auth module",
    " 2. Add tests",
    "────────────────────────────────────────────────",
    "❯",
    "────────────────────────────────────────────────",
]

# Real menus render inside box-drawing borders.
BOXED_PERMISSION_PROMPT = [
    "╭──────────────────────────────────────────────╮",
    "│ Do you want to proceed?                      │",
    "│ ❯ 1. Yes                                     │",
    "│   2. No, and tell Claude what to do          │",
    "╰──────────────────────────────────────────────╯",
]


def test_numbered_menu_detected():
    menu = detect_menu(PERMISSION_PROMPT)
    assert menu is not None
    assert menu.options == (
        "Yes",
        "Yes, and don't ask again this session",
        "No, and tell Claude what to do differently (esc)",
    )
    assert menu.selected == 0
    assert menu.title == "Do you want to proceed?"


def test_unnumbered_menu_detected():
    menu = detect_menu(MODEL_PICKER)
    assert menu is not None
    assert menu.options == ("Opus", "Sonnet", "Haiku")
    assert menu.selected == 1
    assert menu.title == "Select model:"


def test_real_claude_idle_prompt_is_not_a_menu():
    assert detect_menu(REAL_IDLE_SCREEN) is None
    assert is_idle(REAL_IDLE_SCREEN) is True


def test_non_idle_screen_has_no_empty_composer_between_rules():
    assert is_idle(["✻ Working…", "esc to interrupt"]) is False


def test_historical_prompt_cursors_are_not_menus():
    assert detect_menu(REAL_IDLE_WITH_PROMPT_HISTORY) is None


def test_model_scope_prompt_is_blocking_but_not_a_generic_menu():
    assert detect_menu(MODEL_SCOPE_PROMPT_2_1_227) is None
    assert is_model_scope_prompt(MODEL_SCOPE_PROMPT_2_1_227) is True
    assert is_idle(MODEL_SCOPE_PROMPT_2_1_227) is False
    assert detect_model(MODEL_SCOPE_PROMPT_2_1_227) == "haiku"
    assert has_empty_composer(MODEL_SCOPE_PROMPT_2_1_227) is False


def test_historical_prompt_block_is_not_an_unnumbered_menu():
    lines = [
        "~/converse-code/tmp/cc-smoke-project",
        " History:",
        " ❯ List files only in the current working directory.",
        "   Listed directory contents",
        "",
    ]
    assert detect_menu(lines) is None


def test_empty_composer_accepts_new_footer_without_plain_neighbor_rule():
    assert has_empty_composer(["status footer", "❯", "─ ? for shortcuts"])
    assert not has_empty_composer(["❯ previous prompt", "─ ? for shortcuts"])
    assert not has_empty_composer([
        "❯", "Old assistant output", "❯ developer draft",
        "─ ? for shortcuts",
    ])
    assert not has_empty_composer([
        "❯", "Old assistant output with no live composer yet",
    ])
    assert has_empty_composer([
        "────────────────", '❯ Try "fix typecheck errors"',
        "────────────────", "· ? for shortcuts",
    ])
    assert has_empty_composer([
        "❯", "────────────────", "* Cooked for 1s", "? for shortcuts",
    ])
    assert has_empty_composer([
        "❯", "────────────────", "✻ Baked for 1s",
        "? for shortcuts · ↳ Respond with pong",
    ])
    assert has_empty_composer([
        "❯", "────────────────", "✢ Brewed for 1s",
        "? for shortcuts ·",
    ])


def test_model_scope_prompt_can_be_far_from_bottom_of_screen():
    lines = MODEL_SCOPE_PROMPT_2_1_227 + [f"old row {i}" for i in range(20)]
    assert is_model_scope_prompt(lines) is True


def test_model_scope_detector_handles_transient_status_redraw():
    assert is_model_scope_prompt([
        "⏸ manual mode on · ? for shortcuts · his sessio  only · Esc to cancel",
    ]) is True


def test_model_scope_detector_handles_corrupted_cancel_spacing():
    assert is_model_scope_prompt([
        "manual mode · his sessio  onlyp·nEscotoacancel",
    ]) is True


def test_model_scope_detector_requires_cancel_hint():
    assert is_model_scope_prompt([
        "Enter to set as default · s to use this session only",
    ]) is False


def test_numbered_model_row_survives_spinner_overwrite():
    lines = [
        "   Select model",
        "     3. Fable       Fable 5",
        "✻    4. Sonnet      Sonnet 5",
        "   ❯ 5. Haiku ✔     Haiku 4.5",
    ]
    menu = detect_menu(lines)
    assert menu is not None
    assert menu.options == ("Fable       Fable 5", "Sonnet      Sonnet 5", "Haiku ✔     Haiku 4.5")
    assert match_option(menu, "sonnet") == 1

    corrupted = Menu("Select model", ("Sonn t      Sonn t 5", "Haiku"), 1)
    assert match_option(corrupted, "sonnet") == 0


def test_model_detected_from_claude_header():
    assert detect_model([
        "│   Haiku 4.5 · Claude Max · Ronan McGovern   │",
    ]) == "haiku"
    assert detect_model(["Claude says Sonnet is nice"]) is None


def test_current_model_detected_from_visible_claude_state():
    assert detect_model(WELCOME_WITH_MODEL) == "sonnet"
    assert detect_model(REAL_IDLE_WITH_PROMPT_HISTORY) == "fable"
    assert detect_current_model(WELCOME_WITH_MODEL) == "sonnet"
    assert detect_current_model(REAL_IDLE_WITH_PROMPT_HISTORY) is None
    assert detect_current_model(MODEL_PICKER) == "sonnet"


def test_model_acknowledgements_preserve_observed_scope():
    assert model_acknowledgements([
        "  ⎿  Set model to Opus 5 and saved as your default for new sessions",
        "  ⎿  Set model to Sonnet 5 for this session",
    ]) == (
        ModelAcknowledgement("opus", "default_for_new_sessions"),
        ModelAcknowledgement("sonnet", "current_session"),
    )


def test_numbered_prose_is_not_a_menu():
    assert detect_menu(PROSE_WITH_NUMBERS) is None


def test_historical_numbered_menu_above_live_composer_is_not_active():
    assert detect_menu([
        "Do you want to proceed?",
        "❯ 1. Yes",
        "  2. No",
        "────────────────",
        "❯",
        "────────────────",
    ]) is None


def test_boxed_permission_prompt():
    menu = detect_menu(BOXED_PERMISSION_PROMPT)
    assert menu is not None
    assert menu.options == ("Yes", "No, and tell Claude what to do")
    assert menu.selected == 0
    assert menu.title == "Do you want to proceed?"


def test_match_option_exact_prefix_substring_and_number():
    menu = detect_menu(PERMISSION_PROMPT)
    assert match_option(menu, "Yes") == 0
    assert match_option(menu, "yes, and don't ask") == 1
    assert match_option(menu, "tell claude what to do") == 2
    assert match_option(menu, "3") == 2
    assert match_option(menu, "banana") is None
