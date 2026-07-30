#!/usr/bin/env python3
"""UserPromptSubmit hook — state the Hangul tool-arg corruption failure mode once
per session, so it is in context before any Korean is written to a tool argument.

Emits on the FIRST user prompt of each session_id and stays silent afterwards.
Fails open (exit 0, no output) on every error path: a guard must never be able to
block someone's session.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time

CAP = 9000                      # documented hook output limit is 10,000 chars
MARKER_TTL = 14 * 24 * 3600
PAYLOAD_REL = os.path.join("reference", "escape-encoded-korean.md")

HEADER = (
    "Injected by the korean-toolarg-guard plugin on the first prompt of this session. "
    "Measured facts that apply whenever Korean goes into a tool-call argument:\n\n"
)


def fail_open():
    sys.exit(0)


def resolve_state(arg):
    cands = []
    if arg:
        cands.append(arg)
    cands.append(os.path.join(os.path.expanduser("~"), ".claude", "plugins",
                              "data", "korean-toolarg-guard"))
    cands.append(os.path.join(tempfile.gettempdir(), "korean-toolarg-guard"))
    for c in cands:
        try:
            d = os.path.join(c, "sessions")
            os.makedirs(d, exist_ok=True)
            return d
        except Exception:
            continue
    return None


def strip_frontmatter(text):
    if text.startswith("---"):
        m = re.match(r"^---\n.*?\n---\n", text, re.DOTALL)
        if m:
            return text[m.end():]
    return text


def main():
    if os.environ.get("KOREAN_GUARD_DISABLE") == "1":
        sys.exit(0)                       # off-switch: benchmarking, or opt-out per shell

    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--state", default="")
    args, _ = ap.parse_known_args()

    root = args.root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    payload = os.path.join(root, PAYLOAD_REL)
    if not os.path.isfile(payload):
        fail_open()

    try:
        stdin = json.load(sys.stdin)
    except Exception:
        fail_open()
    sid = stdin.get("session_id")
    if not isinstance(sid, str) or not sid.strip():
        fail_open()
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", sid)[:128]

    state = resolve_state(args.state)
    if not state:
        fail_open()
    marker = os.path.join(state, sid)
    if os.path.exists(marker):
        sys.exit(0)

    try:
        with open(payload, encoding="utf-8") as f:
            body = strip_frontmatter(f.read()).strip()
    except Exception:
        fail_open()
    if not body:
        fail_open()

    # claim the session before emitting so a duplicate invocation cannot double-inject
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        sys.exit(0)
    except Exception:
        fail_open()

    ctx = HEADER + body
    if len(ctx) > CAP:
        ctx = ctx[:CAP] + f"\n\n(이하 생략 — 전문은 {payload})"

    try:
        now = time.time()
        for name in os.listdir(state):
            p = os.path.join(state, name)
            if now - os.path.getmtime(p) > MARKER_TTL:
                os.unlink(p)
    except Exception:
        pass

    json.dump({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                      "additionalContext": ctx}},
              sys.stdout, ensure_ascii=False)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        fail_open()
