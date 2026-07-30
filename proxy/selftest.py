#!/usr/bin/env python3
"""Offline proof that escaped Korean is distinguishable from raw Korean.

The whole design rests on this one claim, so it is worth checking without spending any
API budget: after decoding an SSE event once, the model's own spelling survives, and the
transport's own escaping does not hide the signal.

Synthesises the cases through the real Tee class. No network, no config changes.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tee_proxy as T  # noqa: E402

BS = chr(92)


class Args:
    log = ""
    verbose = False
    flag_dir = ""            # flag writing is exercised live, not here
    flag_min = 1


T.ARGS = Args()
CAPTURED = []
T.log_record = CAPTURED.append


def sse(ev):
    """Serialise like the wire does. ensure_ascii=True deliberately mimics a server that
    escapes every non-ASCII character — the case that could have hidden the signal."""
    return ("data: " + json.dumps(ev, ensure_ascii=True) + "\n\n").encode()


def run(tool, chunks):
    CAPTURED.clear()
    t = T.Tee()
    t.feed(sse({"type": "content_block_start", "index": 0,
                "content_block": {"type": "tool_use", "name": tool, "id": "toolu_x"}}))
    for c in chunks:
        t.feed(sse({"type": "content_block_delta", "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": c}}))
    t.feed(sse({"type": "content_block_stop", "index": 0}))
    return CAPTURED[0] if CAPTURED else None


SENT = "파라미터를 추출해 계층별 평균을 갱신한다"
ok = True
print("escape-vs-raw detection selftest\n")

r = run("mcp__x__write", ['{"content": "', SENT, '"}'])
good = r and r["raw_hangul"] > 0 and r["escaped_hangul"] == 0
ok &= bool(good)
print(f"  {'PASS' if good else 'FAIL'}  raw UTF-8        "
      f"raw={r['raw_hangul']:3d} escaped={r['escaped_hangul']:3d}")

esc = "".join(f"{BS}u{ord(c):04x}" for c in SENT)
r = run("mcp__x__write", ['{"content": "', esc, '"}'])
good = r and r["escaped_hangul"] > 0 and r["raw_hangul"] == 0
ok &= bool(good)
print(f"  {'PASS' if good else 'FAIL'}  escaped          "
      f"raw={r['raw_hangul']:3d} escaped={r['escaped_hangul']:3d}")

mixed = '{"a": "파라미터", "b": "' + BS + "ucd94" + BS + 'uc790"}'
r = run("AskUserQuestion", [mixed[:14], mixed[14:22], mixed[22:]])
good = r and r["raw_hangul"] == 4 and r["escaped_hangul"] == 2
ok &= bool(good)
print(f"  {'PASS' if good else 'FAIL'}  mixed, split mid-escape  "
      f"raw={r['raw_hangul']:3d} escaped={r['escaped_hangul']:3d}")

r = run("mcp__x__write", ['{"content": "자동으' + BS + '로 닫힌다"}'])
good = r and r["invalid_escapes"] == 1
ok &= bool(good)
print(f"  {'PASS' if good else 'FAIL'}  invalid: backslash + raw char   "
      f"invalid={r['invalid_escapes']:3d}   (claude-code#79339)")

r = run("AskUserQuestion", ['{"label": "' + BS + 'u洞' + BS + 'uc튼"}'])
good = r and r["invalid_escapes"] == 2
ok &= bool(good)
print(f"  {'PASS' if good else 'FAIL'}  invalid: bad hex digits         "
      f"invalid={r['invalid_escapes']:3d}   (observed locally)")

r = run("mcp__x__write", ['{"content": "$' + BS + BS + 'underbrace{x}$ 파라미터"}'])
good = r and r["invalid_escapes"] == 0
ok &= bool(good)
print(f"  {'PASS' if good else 'FAIL'}  LaTeX not flagged               "
      f"invalid={r['invalid_escapes']:3d}   (doubled backslash is valid)")

r = run("Bash", ['{"command": "ls -la"}'])
good = r is None
ok &= bool(good)
print(f"  {'PASS' if good else 'FAIL'}  no Korean, no record            "
      f"record={'none' if r is None else r}")

r = run("mcp__x__write", ['{"content": "파라미터"}'])
good = r is not None and not any(
    k in json.dumps(r).lower()
    for k in ("authorization", "api_key", "x-api-key", "bearer"))
ok &= bool(good)
print(f"  {'PASS' if good else 'FAIL'}  no credentials in record        "
      f"fields={sorted(r.keys())}")

print("\n" + ("ALL PASS" if ok else "PROBLEM"))
sys.exit(0 if ok else 1)
