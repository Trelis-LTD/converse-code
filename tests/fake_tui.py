"""A tiny scripted stand-in for the Claude Code TUI, driven over a pty in tests."""

import sys


def main() -> None:
    print("Welcome to Fake Claude")
    while True:
        sys.stdout.write("> ")
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
        elif cmd:
            print(f"echo: {cmd}")


if __name__ == "__main__":
    main()
