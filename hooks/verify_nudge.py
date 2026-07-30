#!/usr/bin/env python3
"""PreToolUse hook — when a call about to run carries a substantial amount of Korean
in its arguments, note that fact next to the tool result so the content gets re-read.

Scoped to MCP tools by the matcher in hooks.json: those write to external services
(Notion, Linear, Slack, ...) where corrupted text lands unseen. Local Write/Edit are
left alone because the user reviews those in diffs.

Tunables (environment):
  KOREAN_GUARD_MIN_HANGUL   minimum Hangul chars to say anything   (default 150)
  KOREAN_GUARD_MAX_NUDGES   per-session ceiling, 0 disables        (default 6)

Fails open (exit 0, no output) on every error path.
"""
import argparse
import json
import os
import re
import sys
import tempfile

HANGUL = re.compile(r"[가-힣]")


def fail_open():
    sys.exit(0)


def env_int(name, default):
    try:
        v = int(os.environ.get(name, ""))
        return v if v >= 0 else default
    except Exception:
        return default


def resolve_state(arg):
    cands = []
    if arg:
        cands.append(arg)
    cands.append(os.path.join(os.path.expanduser("~"), ".claude", "plugins",
                              "data", "korean-toolarg-guard"))
    cands.append(os.path.join(tempfile.gettempdir(), "korean-toolarg-guard"))
    for c in cands:
        try:
            d = os.path.join(c, "nudges")
            os.makedirs(d, exist_ok=True)
            return d
        except Exception:
            continue
    return None


def collect(o, out):
    if isinstance(o, str):
        out.append(o)
    elif isinstance(o, dict):
        for v in o.values():
            collect(v, out)
    elif isinstance(o, list):
        for v in o:
            collect(v, out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="")
    args, _ = ap.parse_known_args()

    threshold = env_int("KOREAN_GUARD_MIN_HANGUL", 150)
    max_nudges = env_int("KOREAN_GUARD_MAX_NUDGES", 6)
    if max_nudges == 0:
        sys.exit(0)

    try:
        stdin = json.load(sys.stdin)
    except Exception:
        fail_open()

    tool = stdin.get("tool_name") or "?"
    strings = []
    collect(stdin.get("tool_input") or {}, strings)
    n = len(HANGUL.findall("\n".join(strings)))
    if n < threshold:
        sys.exit(0)

    sid = stdin.get("session_id")
    if not isinstance(sid, str) or not sid.strip():
        fail_open()
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", sid)[:128]

    state = resolve_state(args.state)
    if not state:
        fail_open()
    counter = os.path.join(state, sid)
    try:
        used = int(open(counter, encoding="utf-8").read().strip() or 0) \
            if os.path.exists(counter) else 0
    except Exception:
        used = 0
    if used >= max_nudges:
        sys.exit(0)
    try:
        with open(counter, "w", encoding="utf-8") as f:
            f.write(str(used + 1))
    except Exception:
        pass

    remaining = max_nudges - (used + 1)
    msg = (
        f"`korean-toolarg-guard`: 이 `{tool}` 호출의 인자에 한글 약 {n}자가 들어 있다. "
        "도구 인자의 한글은 적법한 다른 음절로 조용히 바뀔 수 있고(예: 파라미터→파라밌터, "
        "계층→계습), 외부 서비스로 보낸 본문은 아무도 다시 보지 않은 채 남는다. "
        "이 호출이 끝난 뒤 기록된 내용을 되읽어 비단어를 확인하는 것이 이 플러그인이 권하는 절차다. "
        "손상을 찾으면 조용히 덮어쓰지 말고 사용자에게 알린다."
        + (f" (이 세션 알림 {remaining}회 남음)" if remaining > 0
           else " (이 세션의 마지막 알림)")
    )
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                      "additionalContext": msg}},
              sys.stdout, ensure_ascii=False)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        fail_open()
