"""A tiny scripted stand-in for the Claude Code TUI, driven over a pty in tests."""

import sys


def main() -> None:
    print("Welcome to Fake Claude")
    while True:
        sys.stdout.write("────────────────\n❯")
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            return
        cmd = line.strip()
        if cmd == "exit":
            print("bye")
            return
        if cmd == "menu":
            print("Do you want to proceed?")
            print("❯ 1. Yes")
            print("  2. No, and tell Claude what to do differently")
            sys.stdout.flush()
            # A real Claude modal blocks here. Clear its Ink region only after a choice is
            # submitted so the PTY integration exercises a genuine menu transition.
            choice = sys.stdin.readline()
            if not choice:
                return
            sys.stdout.write("\x1b[2J\x1b[H")
            print("choice accepted")
        elif cmd:
            print(f"echo: {cmd}")


if __name__ == "__main__":
    main()
