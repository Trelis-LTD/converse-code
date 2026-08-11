"""PTY child used to verify bounded shutdown escalation."""

import os
import signal
import time


if os.fork() == 0:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(1)

signal.signal(signal.SIGHUP, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("ignoring signals", flush=True)
while True:
    time.sleep(1)
