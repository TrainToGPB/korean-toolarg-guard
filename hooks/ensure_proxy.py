#!/usr/bin/env python3
"""SessionStart hook — bring the detection proxy up so you never start it by hand.

What this can and cannot automate
---------------------------------
Starting the proxy: yes, that is what this hook does.

Routing traffic through it: no. `ANTHROPIC_BASE_URL` is resolved when the CLI starts, and a
plugin's own `settings.json` may only set `agent` and `subagentStatusLine` — it cannot inject
environment variables. So pointing Claude Code at the proxy stays a one-time edit you make:

    "env": {
      "ANTHROPIC_BASE_URL":     "http://127.0.0.1:8099/v1",
      "KOREAN_GUARD_UPSTREAM":  "https://api.anthropic.com/v1"
    }

`KOREAN_GUARD_UPSTREAM` is both the real endpoint the proxy forwards to and the opt-in
signal for this hook: without it the hook exits silently, so installing the plugin never
starts a background process you did not ask for.

Read this before wiring it up
-----------------------------
Once `ANTHROPIC_BASE_URL` points at the proxy, a proxy that is not listening means no API
access at all. This hook exists to make that unlikely — it revives the proxy at the start of
every session — but for an always-on setup prefer a supervised service (launchd `KeepAlive`
on macOS, a systemd user unit on Linux) and leave this as the backstop. To back out, delete
the two env lines.

Tunables:
  KOREAN_GUARD_UPSTREAM   real base URL to forward to  (required; also the opt-in switch)
  KOREAN_GUARD_PORT       listen port                  (default 8099)
  KOREAN_GUARD_FLAG_DIR   findings dir shared with escape_guard
  KOREAN_GUARD_PROXY      set to "off" to skip starting it

Fails open on every path: a hook must never be able to wedge a session.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time

LOCK_TTL = 30


def out(system_message=None):
    """SessionStart may return a systemMessage; anything else stays quiet."""
    if system_message:
        json.dump({"systemMessage": system_message}, sys.stdout, ensure_ascii=False)
    sys.exit(0)


def listening(port):
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def state_dir():
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or \
        os.path.join(os.path.expanduser("~"), ".claude")
    d = os.path.join(cfg, ".state", "escape-guard")
    os.makedirs(d, exist_ok=True)
    return d


def claim(lock):
    """One session wins the right to spawn; the others just wait for the port."""
    try:
        if os.path.exists(lock) and time.time() - os.path.getmtime(lock) > LOCK_TTL:
            os.unlink(lock)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    args, _ = ap.parse_known_args()
    try:
        json.load(sys.stdin)                       # drain stdin; contents unused
    except Exception:
        pass

    if os.environ.get("KOREAN_GUARD_PROXY", "").lower() == "off":
        out()
    upstream = os.environ.get("KOREAN_GUARD_UPSTREAM", "").strip()
    if not upstream:
        out()                                      # not wired up: stay out of the way

    try:
        port = int(os.environ.get("KOREAN_GUARD_PORT", "8099"))
    except ValueError:
        port = 8099

    if listening(port):
        out()                                      # already up, nothing to do

    root = args.root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proxy = os.path.join(root, "proxy", "tee_proxy.py")
    if not os.path.isfile(proxy):
        out(f"korean-toolarg-guard: proxy not found at {proxy}")

    d = state_dir()
    lock = os.path.join(d, ".starting.lock")
    if not claim(lock):
        for _ in range(30):                        # another session is spawning it
            time.sleep(0.2)
            if listening(port):
                out()
        out(f"korean-toolarg-guard: waited for a proxy on port {port} that never came up")

    log = os.path.join(d, "proxy.log")
    cmd = [sys.executable, "-u", proxy, "--upstream", upstream, "--port", str(port),
           "--flag-dir", os.environ.get("KOREAN_GUARD_FLAG_DIR", d)]
    try:
        with open(log, "a", encoding="utf-8") as lf:
            lf.write(f"\n--- starting {time.strftime('%Y-%m-%d %H:%M:%S')} "
                     f"port={port} upstream={upstream}\n")
            subprocess.Popen(cmd, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
    except Exception as e:
        try:
            os.unlink(lock)
        except Exception:
            pass
        out(f"korean-toolarg-guard: could not start the proxy ({type(e).__name__}). "
            f"ANTHROPIC_BASE_URL points at port {port}, so requests will fail until it "
            f"is running. See {log}")

    for _ in range(50):                            # up to ~10s for the port to open
        time.sleep(0.2)
        if listening(port):
            try:
                os.unlink(lock)
            except Exception:
                pass
            out(f"korean-toolarg-guard: escape-detection proxy listening on "
                f"127.0.0.1:{port}")
    try:
        os.unlink(lock)
    except Exception:
        pass
    out(f"korean-toolarg-guard: proxy did not come up on port {port} within 10s — see {log}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
