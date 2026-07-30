#!/usr/bin/env python3
"""Detect escape-encoded Korean in tool-call arguments, on the wire.

Claude Code sometimes spells Korean inside tool arguments as `\\uXXXX` Unicode escapes
rather than emitting the characters directly. That is the failure this proxy exists to
catch, and it cannot be caught from inside the CLI: by the time a hook or a transcript
sees `tool_input`, the harness has already parsed the JSON and the escapes have become
characters. The network is the one vantage point upstream of that parse.

In a streaming response, tool arguments arrive as `content_block_delta` events carrying
`delta.input_json_delta.partial_json`. That field is itself a JSON string, so a backslash
the model emitted is doubled on the wire. Decoding each SSE event once — all this proxy
does — undoes the *transport's* escaping and leaves exactly what the model produced:

    literal  \\ud30c\\ub77c\\ubbf8\\ud130   the model escaped
    literal  파라미터                        the model did not

The proxy streams the response through unchanged. It never rewrites the payload, because
a wrong hex digit decodes to the wrong character either way — repair is not possible here.
Instead it writes a finding keyed by `tool_use_id` into a flag directory, and the
`escape_guard` PreToolUse hook turns that into a refusal before the tool runs.

Logs contain reconstructed tool-input metadata only: never request bodies, never headers,
never credentials.

Usage — point a *separate* shell at it; leave your real settings alone:

    ./tee_proxy.py --upstream https://api.anthropic.com/v1 --port 8099
    ANTHROPIC_BASE_URL=http://127.0.0.1:8099/v1 claude -p '...'

No TLS interception is involved: the CLI speaks plain HTTP to localhost and the proxy
opens its own TLS connection upstream, so no certificate has to be trusted.
"""
import argparse
import http.client
import json
import os
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Hop-by-hop headers must not be forwarded. Everything else, authentication included,
# passes through untouched so upstream sees exactly what the CLI sent.
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}

HANGUL = re.compile(r"[가-힣]")
BS = chr(92)
ESC = re.compile(re.escape(BS) + r"u([0-9a-fA-F]{4})")
HEX = set("0123456789abcdefABCDEF")
JSON_ESC = set('"' + BS + "/bfnrt")

LOCK = threading.Lock()
ARGS = None


def log_record(rec):
    if not ARGS.log:
        return
    with LOCK:
        with open(ARGS.log, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def scan_escapes(raw):
    """Classify every backslash in a raw JSON string, left to right.

    A scan rather than a regex, because `\\\\` is itself a valid escape and a regex would
    misread the second backslash of a pair as starting a new sequence. This also keeps
    LaTeX honest: inside a JSON string `\\underbrace` is spelled `\\\\underbrace`, so the
    pair is consumed and the rest is literal text — no false positive.

    Returns (hangul codepoints seen as escapes, jamo count, invalid fragments).

    `invalid` covers both leak shapes observed in the wild: a backslash followed directly
    by a raw character (`\\로`, anthropics/claude-code#79339) and a `\\u` whose four
    "digits" are not hex at all (`\\u洞`).
    """
    hangul, jamo, invalid = [], 0, []
    i, n = 0, len(raw)
    while i < n:
        if raw[i] != BS:
            i += 1
            continue
        if i + 1 >= n:
            invalid.append(raw[i:])
            break
        c = raw[i + 1]
        if c == "u":
            h = raw[i + 2:i + 6]
            if len(h) == 4 and all(ch in HEX for ch in h):
                cp = int(h, 16)
                if 0xAC00 <= cp <= 0xD7A3:
                    hangul.append(cp)
                elif 0x1100 <= cp <= 0x11FF or 0x3130 <= cp <= 0x318F:
                    jamo += 1
                i += 6
                continue
            invalid.append(raw[i:i + 6])
            i += 2
            continue
        if c in JSON_ESC:
            i += 2                          # a legitimate escape, `\\` pair included
            continue
        invalid.append(raw[i:i + 2])        # backslash + raw character
        i += 2
    return hangul, jamo, invalid


def classify(raw):
    """How was the Korean in this tool input spelled?

    `escaped_hangul` and `raw_hangul` are measured on the same string, so their ratio
    answers the question directly.
    """
    esc_hangul, esc_jamo, invalid = scan_escapes(raw)
    return {
        "raw_len": len(raw),
        "raw_hangul": len(HANGUL.findall(raw)),
        "escaped_hangul": len(esc_hangul),
        "escaped_jamo": esc_jamo,
        "invalid_escapes": len(invalid),
        "invalid_samples": invalid[:5],
    }


def write_flag(tool_use_id, rec):
    """Hand the finding to the PreToolUse hook, keyed by tool_use_id.

    The proxy can see the escapes but cannot stop a call; the hook can stop a call but
    cannot see the escapes. This file is the join. It is written before the chunk is
    forwarded, so it always lands before the CLI reaches PreToolUse — no race.
    """
    if not ARGS.flag_dir or not tool_use_id:
        return
    forced = os.environ.get("KOREAN_GUARD_FORCE_FLAG") == "1"
    if rec["escaped_hangul"] < ARGS.flag_min and not forced:
        return
    try:
        os.makedirs(ARGS.flag_dir, exist_ok=True)
        payload = dict(rec, tool_use_id=tool_use_id, forced=forced)
        tmp = os.path.join(ARGS.flag_dir, f".{tool_use_id}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(ARGS.flag_dir, f"{tool_use_id}.json"))
    except Exception:
        pass                                # observation must never break the stream


class Tee:
    """Accumulate partial_json per content block; report finished tool inputs."""

    def __init__(self):
        self.buf = b""
        self.blocks = {}

    def feed(self, chunk):
        self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            self._line(line.decode("utf-8", "replace").rstrip("\r"))

    def _line(self, line):
        if not line.startswith("data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return
        try:
            ev = json.loads(payload)        # one decode: undoes transport escaping only
        except Exception:
            return
        t = ev.get("type")
        if t == "content_block_start":
            cb = ev.get("content_block") or {}
            if cb.get("type") == "tool_use":
                self.blocks[ev.get("index")] = {"name": cb.get("name"),
                                                "id": cb.get("id"), "raw": []}
        elif t == "content_block_delta":
            d = ev.get("delta") or {}
            if d.get("type") == "input_json_delta":
                b = self.blocks.get(ev.get("index"))
                if b is not None and isinstance(d.get("partial_json"), str):
                    b["raw"].append(d["partial_json"])
        elif t == "content_block_stop":
            b = self.blocks.pop(ev.get("index"), None)
            if not b:
                return
            raw = "".join(b["raw"])
            if not raw:
                return
            info = classify(raw)
            if (info["raw_hangul"] == 0 and info["escaped_hangul"] == 0
                    and info["invalid_escapes"] == 0):
                return                      # nothing Korean, nothing broken
            rec = {"tool": b["name"], **info}
            m = ESC.search(raw)
            if m:
                # a short excerpt so a finding stays traceable, never the whole payload
                rec["escape_sample"] = raw[max(0, m.start() - 40):m.end() + 40]
            log_record(rec)
            write_flag(b.get("id"), rec)
            if ARGS.verbose:
                print(f"  [tee] {rec['tool']}: raw_hangul={rec['raw_hangul']} "
                      f"escaped_hangul={rec['escaped_hangul']} "
                      f"invalid={rec['invalid_escapes']}", file=sys.stderr, flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _proxy(self, method):
        up = urllib.parse.urlparse(ARGS.upstream)
        path = self.path
        # the CLI appends its own suffix to ANTHROPIC_BASE_URL; strip our mount so the
        # upstream base path appears exactly once
        if ARGS.mount and path.startswith(ARGS.mount):
            path = path[len(ARGS.mount):]
        target = up.path.rstrip("/") + path

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        headers["Host"] = up.hostname

        cls = (http.client.HTTPSConnection if up.scheme == "https"
               else http.client.HTTPConnection)
        conn = cls(up.hostname, up.port or (443 if up.scheme == "https" else 80),
                   timeout=ARGS.timeout)
        try:
            conn.request(method, target, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception as e:
            msg = f"tee_proxy upstream error: {type(e).__name__}: {e}".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return

        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in HOP:
                self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        tee = Tee()
        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                try:
                    tee.feed(chunk)         # observe before forwarding: flag wins the race
                except Exception:
                    pass
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()

    def do_POST(self):
        self._proxy("POST")

    def do_GET(self):
        self._proxy("GET")


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", required=True,
                    help="real base URL, e.g. https://api.anthropic.com/v1")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--mount", default="",
                    help="path prefix the CLI sends; defaults to the upstream's path")
    ap.add_argument("--log", default="", help="JSONL findings log (optional)")
    ap.add_argument("--flag-dir",
                    default=os.path.join(os.path.expanduser("~"), ".claude", ".state",
                                         "escape-guard"),
                    help="where to drop findings for the escape_guard hook")
    ap.add_argument("--flag-min", type=int, default=1,
                    help="escaped Hangul syllables needed to flag a call")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--verbose", action="store_true")
    ARGS = ap.parse_args()
    if not ARGS.mount:
        ARGS.mount = urllib.parse.urlparse(ARGS.upstream).path.rstrip("/")

    print(f"tee proxy  :  http://127.0.0.1:{ARGS.port}{ARGS.mount}")
    print(f"upstream   :  {ARGS.upstream}")
    print(f"flags      :  {ARGS.flag_dir}  (min escaped hangul: {ARGS.flag_min})")
    if ARGS.log:
        print(f"log        :  {ARGS.log}  (tool inputs only; no headers, no credentials)")
    if os.environ.get("KOREAN_GUARD_FORCE_FLAG") == "1":
        print("             KOREAN_GUARD_FORCE_FLAG=1 — flagging every Korean call "
              "(plumbing test only)")
    print(f"\n  ANTHROPIC_BASE_URL=http://127.0.0.1:{ARGS.port}{ARGS.mount}")
    ThreadingHTTPServer(("127.0.0.1", ARGS.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
