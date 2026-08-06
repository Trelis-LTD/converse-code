from converse_code.screen import detect_menu, match_option

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

IDLE_SCREEN = [
    " Claude finished the task. All tests pass.",
    "",
    " > ",
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
    assert menu.options == [
        "Yes",
        "Yes, and don't ask again this session",
        "No, and tell Claude what to do differently (esc)",
    ]
    assert menu.selected == 0
    assert menu.title == "Do you want to proceed?"


def test_unnumbered_menu_detected():
    menu = detect_menu(MODEL_PICKER)
    assert menu is not None
    assert menu.options == ["Opus", "Sonnet", "Haiku"]
    assert menu.selected == 1
    assert menu.title == "Select model:"


def test_idle_screen_is_not_a_menu():
    assert detect_menu(IDLE_SCREEN) is None


def test_real_claude_idle_prompt_is_not_a_menu():
    assert detect_menu(REAL_IDLE_SCREEN) is None


def test_numbered_prose_is_not_a_menu():
    assert detect_menu(PROSE_WITH_NUMBERS) is None


def test_boxed_permission_prompt():
    menu = detect_menu(BOXED_PERMISSION_PROMPT)
    assert menu is not None
    assert menu.options == ["Yes", "No, and tell Claude what to do"]
    assert menu.selected == 0
    assert menu.title == "Do you want to proceed?"


def test_match_option_exact_prefix_substring_and_number():
    menu = detect_menu(PERMISSION_PROMPT)
    assert match_option(menu, "Yes") == 0
    assert match_option(menu, "yes, and don't ask") == 1
    assert match_option(menu, "tell claude what to do") == 2
    assert match_option(menu, "3") == 2
    assert match_option(menu, "banana") is None
