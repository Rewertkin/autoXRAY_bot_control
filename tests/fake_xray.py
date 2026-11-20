#!/usr/bin/env python3
"""Имитация CLI xray для тестов. Пишет вызовы в $FAKE_XRAY_LOG, ответы берёт из $FAKE_XRAY_DIR."""

import json
import os
import sys
from pathlib import Path

raw = sys.argv[1:]
state = Path(os.environ["FAKE_XRAY_DIR"])
with open(state / "calls.log", "a") as f:
    f.write(" ".join(raw) + "\n")

if raw[:1] == ["api"]:
    # Как пакет flag в Go: после первого позиционного аргумента всё считается позиционным.
    positional_started = False
    for a in raw[2:]:
        if positional_started and a.startswith("-"):
            print(f"failed to load {a}: open {a}: no such file or directory", file=sys.stderr)
            sys.exit(1)
        if not a.startswith("-") and not positional_started:
            # значение флага вида "-pattern X" не является позиционным
            prev = raw[raw.index(a) - 1] if raw.index(a) > 0 else ""
            if not (prev.startswith("-") and "=" not in prev and prev not in ("-reset", "-all", "-include-traffic")):
                positional_started = True
    if not any(a.startswith("--server=") for a in raw):
        print("failed to dial 127.0.0.1:8080", file=sys.stderr)
        sys.exit(1)

args = [a for a in raw if not a.startswith("--server=")]

if args[:1] == ["version"]:
    print("Xray 26.11.9 (Xray, Penetrates Everything.)")
elif args[:2] == ["run", "-test"]:
    json.loads(Path(args[-1]).read_text())
    print("Configuration OK.")
elif args[:2] == ["api", "adu"]:
    payload = json.loads(Path(args[2]).read_text())
    for inb in payload["inbounds"]:
        for c in inb["settings"]["clients"]:
            print("add user:", c["email"])
    print("Added 1 user(s) in total.")
elif args[:2] == ["api", "rmu"]:
    print("remove user:", args[-1])
    print("Removed 1 user(s) in total.")
elif args[:2] == ["api", "statsquery"]:
    p = state / "stats.json"
    print(p.read_text() if p.exists() else "{}")
    if "-reset" in args and p.exists():
        p.unlink()
elif args[:2] == ["api", "statsonlineiplist"]:
    p = state / "online.json"
    print(p.read_text() if p.exists() else "{}")
else:
    print("unknown", args, file=sys.stderr)
    sys.exit(1)
