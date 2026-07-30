#!/usr/bin/env python3
"""PreToolUse hook — refuse a tool call whose Korean arrived as Unicode escapes.

Division of labour, and why it needs two pieces:

  the proxy  sees the escapes (it sits upstream of the harness's JSON parse) but cannot
             stop a call
  this hook  can stop a call before it executes but cannot see the escapes, because
             `tool_input` reaches it already decoded

They are joined by `tool_use_id`: the proxy writes a finding to a flag directory keyed by
that id, and this hook looks it up. Without the proxy running the directory stays empty and
this hook does nothing at all, so it is inert for anyone not measuring.

Why refuse rather than warn: spelling Korean as escapes is itself the defect, regardless of
whether the digits happened to come out right. Each syllable becomes several hex tokens, so
a long passage is hundreds of independent chances to slip, and the model cannot read its own
output back as meaning while it is producing it.

Tunables (environment):
  KOREAN_GUARD_FLAG_DIR     where the proxy drops findings
  KOREAN_GUARD_ESCAPE_MIN   escaped Hangul syllables needed to refuse   (default 1)
  KOREAN_GUARD_MAX_DENY     refusals per session before giving up       (default 2)

Fails open on every error path: a guard must never be able to wedge a session.
"""
import json
import os
import re
import sys
import time

BS = chr(92)
FLAG_TTL = 900                      # forget findings the CLI never came back for


def fail_open():
    sys.exit(0)


def env_int(name, default):
    try:
        v = int(os.environ.get(name, ""))
        return v if v >= 0 else default
    except Exception:
        return default


def flag_dir():
    d = os.environ.get("KOREAN_GUARD_FLAG_DIR")
    if d:
        return d
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or \
        os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(cfg, ".state", "escape-guard")


def prune(d):
    try:
        now = time.time()
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if now - os.path.getmtime(p) > FLAG_TTL:
                os.unlink(p)
    except Exception:
        pass


def main():
    if os.environ.get("KOREAN_GUARD_DISABLE") == "1":
        sys.exit(0)

    try:
        stdin = json.load(sys.stdin)
    except Exception:
        fail_open()

    tuid = stdin.get("tool_use_id")
    sid = stdin.get("session_id") or "nosession"
    tool = stdin.get("tool_name") or "?"
    if not isinstance(tuid, str) or not tuid:
        fail_open()

    d = flag_dir()
    if not os.path.isdir(d):
        sys.exit(0)                     # proxy not running: nothing to do

    path = os.path.join(d, f"{re.sub(r'[^A-Za-z0-9_-]', '_', tuid)[:128]}.json")
    if not os.path.exists(path):
        sys.exit(0)                     # this call was clean

    try:
        with open(path, encoding="utf-8") as f:
            flag = json.load(f)
    except Exception:
        fail_open()
    finally:
        try:
            os.unlink(path)              # consume it either way
        except Exception:
            pass
    prune(d)

    escaped = int(flag.get("escaped_hangul") or 0)
    raw_ko = int(flag.get("raw_hangul") or 0)
    invalid = int(flag.get("invalid_escapes") or 0)
    forced = bool(flag.get("forced"))
    if escaped < env_int("KOREAN_GUARD_ESCAPE_MIN", 1) and not forced:
        sys.exit(0)

    # Retries arrive with a fresh tool_use_id, so the ceiling has to be per session.
    cap = env_int("KOREAN_GUARD_MAX_DENY", 2)
    counter = os.path.join(d, f"_deny_{re.sub(r'[^A-Za-z0-9_-]', '_', sid)[:80]}.count")
    try:
        used = int(open(counter, encoding="utf-8").read().strip() or 0) \
            if os.path.exists(counter) else 0
    except Exception:
        used = 0

    if used >= cap:
        # Stop refusing rather than loop forever; let it through with the finding attached.
        msg = (f"korean-toolarg-guard: this `{tool}` call again escape-encoded "
               f"{escaped} Hangul syllables. The per-session denial cap ({cap}) has been "
               f"reached, so the call is allowed through. Re-read the delivered Korean "
               f"for non-words, and tell the user if anything is corrupted rather than "
               f"silently overwriting it.")
        json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                          "additionalContext": msg}},
                  sys.stdout, ensure_ascii=False)
        sys.exit(0)

    try:
        with open(counter, "w", encoding="utf-8") as f:
            f.write(str(used + 1))
    except Exception:
        pass

    detail = (f"{escaped} Hangul syllables in this call's arguments were written as "
              f"{BS}uXXXX Unicode escapes")
    if raw_ko:
        detail += f" ({raw_ko} arrived as raw characters)"
    if invalid:
        detail += f", and {invalid} escape sequence(s) were malformed"

    reason = (
        f"Escape-encoded Korean detected — {detail}. "
        f"Write tool arguments as raw text: put the Korean characters in directly, never "
        f"as {BS}uXXXX. Escape spelling needs four correct hex digits per syllable, so a "
        f"long passage is hundreds of independent chances to slip, and the model cannot "
        f"read its own output back as meaning while producing it. "
        f"Re-emit the same content without escapes and call the tool again."
    )
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                      "permissionDecision": "deny",
                                      "permissionDecisionReason": reason}},
              sys.stdout, ensure_ascii=False)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        fail_open()
