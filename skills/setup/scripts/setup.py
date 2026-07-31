#!/usr/bin/env python3
"""Mechanics for the korean-toolarg-guard setup skill.

Each subcommand does one reversible step and prints what it did, so the skill can show the
user real output instead of claiming success. Nothing here edits live configuration except
`wire`, and `wire` always writes a backup first.

  detect            report the current state as JSON: platform, config dir, current
                    endpoint, whether already wired, a free port, service manager
  render-service    write the launchd plist / systemd unit to a path and print it,
                    without installing anything
  install-service   install and start the supervised service, wait for the port
  dry-run           prove a request works through the proxy using a throwaway settings
                    copy, before any live configuration is touched
  wire              point the real settings.json at the proxy (backup first)
  status            service state, port, findings so far
  uninstall         stop the service and remove the two env keys, leaving a backup
"""
import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse

LABEL = "com.korean-toolarg-guard.proxy"
KEYS = ("ANTHROPIC_BASE_URL", "KOREAN_GUARD_UPSTREAM", "KOREAN_GUARD_PORT",
        "KOREAN_GUARD_FLAG_DIR", "KOREAN_GUARD_BASE_WAS_UNSET")
# No "/v1": the CLI appends "/v1/messages" to whatever ANTHROPIC_BASE_URL says, so an
# upstream carrying "/v1" produces "/v1/v1/messages" — a 404 the CLI then reports as
# "issue with the selected model", which is a long way from the truth.
DEFAULT_UPSTREAM = "https://api.anthropic.com"


def cfg_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or \
        os.path.join(os.path.expanduser("~"), ".claude")


def settings_path():
    return os.path.join(cfg_dir(), "settings.json")


def load_settings():
    p = settings_path()
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def state_dir():
    d = os.path.join(cfg_dir(), ".state", "escape-guard")
    os.makedirs(d, exist_ok=True)
    return d


def listening(port, timeout=0.4):
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


def free_port(start=8099, tries=40):
    for p in range(start, start + tries):
        if not listening(p, 0.15):
            return p
    return start


def plugin_root(explicit=""):
    if explicit:
        return explicit
    # scripts/ -> setup/ -> skills/ -> plugin root
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))


def proxy_path(root):
    return os.path.join(root, "proxy", "tee_proxy.py")


def service_manager():
    if platform.system() == "Darwin":
        return "launchd" if shutil.which("launchctl") else "none"
    if shutil.which("systemctl"):
        r = subprocess.run(["systemctl", "--user", "is-system-running"],
                           capture_output=True, text=True)
        if r.returncode == 0 or "running" in (r.stdout + r.stderr):
            return "systemd"
    return "none"


# ── endpoint arithmetic ──────────────────────────────────────────────────────
def is_local(url):
    return url.startswith("http://127.0.0.1") or url.startswith("http://localhost")


def resolve_upstream(env, fallback=DEFAULT_UPSTREAM):
    """The endpoint the proxy must forward to.

    The endpoint they are on right now IS the upstream. Only fall back to the flag (and
    then the public default) when there is nothing to preserve — getting this wrong would
    silently redirect a company gateway to api.anthropic.com and lose the original on
    uninstall.
    """
    if env.get("KOREAN_GUARD_UPSTREAM"):
        return env["KOREAN_GUARD_UPSTREAM"]
    base = env.get("ANTHROPIC_BASE_URL", "")
    if base and not is_local(base):
        return base
    return fallback


def proxy_base_url(port, upstream):
    """Where ANTHROPIC_BASE_URL must point for a proxy fronting `upstream`.

    The proxy mounts itself at the upstream's path and strips that prefix before
    forwarding (see tee_proxy.py), so the mount has to be *exactly* the upstream's path —
    "/v1/claude-code" for a gateway, and empty for a bare host. Inventing a "/v1" when the
    upstream has no path bricks the machine: every request 404s and the CLI blames the
    model, so the settings.json that caused it is the last place anyone looks.
    """
    mount = urllib.parse.urlsplit(upstream).path.rstrip("/")
    return f"http://127.0.0.1:{port}{mount}"


# ── detect ───────────────────────────────────────────────────────────────────
def cmd_detect(a):
    s = load_settings()
    env = s.get("env") or {}
    base = env.get("ANTHROPIC_BASE_URL", "")
    wired = is_local(base)
    upstream = resolve_upstream(env, a.upstream)
    port = None
    if wired:
        try:
            port = int(base.split("//", 1)[1].split("/", 1)[0].rsplit(":", 1)[1])
        except Exception:
            port = None
    use_port = port or free_port()
    root = plugin_root(a.root)
    info = {
        "platform": platform.system(),
        "config_dir": cfg_dir(),
        "settings_exists": os.path.exists(settings_path()),
        "settings_has_env_base_url": bool(base),
        "current_base_url": base or "(unset — CLI default)",
        "already_wired": wired,
        "upstream_to_use": upstream,
        "base_url_to_write": proxy_base_url(use_port, upstream),
        "port": use_port,
        "port_in_use": listening(port or 0) if port else False,
        "service_manager": service_manager(),
        "plugin_root": root,
        "proxy_present": os.path.isfile(proxy_path(root)),
        "flag_dir": env.get("KOREAN_GUARD_FLAG_DIR") or state_dir(),
        "service_installed": service_installed(),
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def service_installed():
    if platform.system() == "Darwin":
        return os.path.exists(os.path.join(
            os.path.expanduser("~"), "Library", "LaunchAgents", LABEL + ".plist"))
    return os.path.exists(os.path.join(
        os.path.expanduser("~"), ".config", "systemd", "user",
        "korean-toolarg-guard.service"))


# ── service files ────────────────────────────────────────────────────────────
def plist_text(py, proxy, port, upstream, flag_dir, log):
    args = "".join(f"    <string>{x}</string>\n" for x in
                   [py, "-u", proxy, "--upstream", upstream, "--port", str(port),
                    "--flag-dir", flag_dir])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{args}  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
"""


def unit_text(py, proxy, port, upstream, flag_dir, log):
    # Without the append: lines the proxy's output goes to journald only, proxy.log is
    # never created, and `status` shows an empty log on the platform where you most need
    # it. Needs systemd 240+; older systemd ignores the directive rather than failing.
    return f"""[Unit]
Description=korean-toolarg-guard escape-detection proxy
After=network-online.target

[Service]
ExecStart={py} -u {proxy} --upstream {upstream} --port {port} --flag-dir {flag_dir}
StandardOutput=append:{log}
StandardError=append:{log}
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
"""


def service_target():
    if platform.system() == "Darwin":
        d = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents")
        return d, os.path.join(d, LABEL + ".plist")
    d = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    return d, os.path.join(d, "korean-toolarg-guard.service")


def build_service(a):
    root = plugin_root(a.root)
    proxy = proxy_path(root)
    if not os.path.isfile(proxy):
        sys.exit(f"proxy not found: {proxy}")
    flag_dir = a.flag_dir or state_dir()
    log = os.path.join(state_dir(), "proxy.log")
    py = sys.executable
    if platform.system() == "Darwin":
        return plist_text(py, proxy, a.port, a.upstream, flag_dir, log)
    return unit_text(py, proxy, a.port, a.upstream, flag_dir, log)


def cmd_render_service(a):
    text = build_service(a)
    _, dest = service_target()
    out = a.out or dest
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(f"# would install to: {dest}")
    print(f"# rendered to     : {out if a.out else '(stdout only)'}")
    print(text)
    return 0


def cmd_install_service(a):
    text = build_service(a)
    d, dest = service_target()
    os.makedirs(d, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"  wrote {dest}")
    uid = os.getuid()
    if platform.system() == "Darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
                       capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", dest],
                           capture_output=True, text=True)
        if r.returncode != 0:
            r = subprocess.run(["launchctl", "load", "-w", dest],
                               capture_output=True, text=True)
        print(f"  launchctl rc={r.returncode} {(r.stderr or '').strip()[:120]}")
    else:
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        r = subprocess.run(["systemctl", "--user", "enable", "--now",
                            "korean-toolarg-guard.service"],
                           capture_output=True, text=True)
        print(f"  systemctl rc={r.returncode} {(r.stderr or '').strip()[:120]}")
    for _ in range(50):
        time.sleep(0.2)
        if listening(a.port):
            print(f"  proxy listening on 127.0.0.1:{a.port}")
            return 0
    print(f"  FAILED: nothing listening on {a.port} after 10s. "
          f"See {os.path.join(state_dir(), 'proxy.log')}")
    return 1


# ── dry run ──────────────────────────────────────────────────────────────────
def cmd_dry_run(a):
    """Prove the proxy works before touching live settings."""
    if not listening(a.port):
        print(f"  FAILED: nothing listening on 127.0.0.1:{a.port}")
        return 1
    s = load_settings()
    env = dict(s.get("env") or {})
    # Must be the exact URL `wire` will write. If the two are computed differently the
    # gate proves nothing about the thing it is gating.
    up = resolve_upstream(env, a.upstream)
    env["ANTHROPIC_BASE_URL"] = proxy_base_url(a.port, up)
    print(f"  upstream: {up}")
    print(f"  base URL: {env['ANTHROPIC_BASE_URL']}")
    tmp = os.path.join(state_dir(), "dryrun-settings.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"env": env, "model": s.get("model", "opus")}, f)
    os.chmod(tmp, 0o600)
    claude = shutil.which("claude") or os.path.join(
        os.path.expanduser("~"), ".local", "bin", "claude")
    try:
        r = subprocess.run([claude, "-p", "reply with exactly: proxy-ok",
                            "--settings", tmp],
                           capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=180)
        out = (r.stdout or r.stderr or "").strip()
    except Exception as e:
        out = f"{type(e).__name__}: {e}"
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass
    ok = "proxy-ok" in out
    print(f"  through-proxy request: {'OK' if ok else 'FAILED'}")
    print(f"  response: {out[:200]}")
    return 0 if ok else 1


# ── wire / uninstall / status ───────────────────────────────────────────────
def cmd_wire(a):
    p = settings_path()
    s = load_settings()
    env = dict(s.get("env") or {})
    base = env.get("ANTHROPIC_BASE_URL", "")
    real = resolve_upstream(env, a.upstream)
    if not base:
        env["KOREAN_GUARD_BASE_WAS_UNSET"] = "1"   # so uninstall removes it, not guesses
    if os.path.exists(p):
        bak = p + f".ktg-backup-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(p, bak)
        os.chmod(bak, 0o600)
        print(f"  backup: {bak}")
    env["KOREAN_GUARD_UPSTREAM"] = real
    env["ANTHROPIC_BASE_URL"] = proxy_base_url(a.port, real)
    env["KOREAN_GUARD_PORT"] = str(a.port)
    s["env"] = env
    with open(p, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
        f.write("\n")
    json.load(open(p, encoding="utf-8"))          # fail loudly if we broke it
    print(f"  ANTHROPIC_BASE_URL    -> {env['ANTHROPIC_BASE_URL']}")
    print(f"  KOREAN_GUARD_UPSTREAM -> {real}")
    print("  settings.json still parses")
    return 0


def cmd_uninstall(a):
    uid = os.getuid()
    _, dest = service_target()
    if platform.system() == "Darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
                       capture_output=True)
        subprocess.run(["launchctl", "unload", "-w", dest], capture_output=True)
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now",
                        "korean-toolarg-guard.service"], capture_output=True)
    if os.path.exists(dest):
        os.unlink(dest)
        print(f"  removed {dest}")
    p = settings_path()
    if os.path.exists(p):
        s = load_settings()
        env = dict(s.get("env") or {})
        real = env.get("KOREAN_GUARD_UPSTREAM")
        was_unset = env.get("KOREAN_GUARD_BASE_WAS_UNSET") == "1"
        bak = p + f".ktg-backup-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(p, bak)
        os.chmod(bak, 0o600)
        for k in KEYS:
            env.pop(k, None)
        if real and not was_unset:
            env["ANTHROPIC_BASE_URL"] = real      # put the original endpoint back
        s["env"] = env
        with open(p, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
            f.write("\n")
        json.load(open(p, encoding="utf-8"))
        print(f"  backup: {bak}")
        print(f"  restored ANTHROPIC_BASE_URL -> {env.get('ANTHROPIC_BASE_URL', '(unset)')}")
    return 0


def cmd_status(a):
    s = load_settings()
    env = s.get("env") or {}
    port = a.port or int(env.get("KOREAN_GUARD_PORT") or 0) or 8099
    d = state_dir()
    flags = [f for f in os.listdir(d) if f.endswith(".json")] if os.path.isdir(d) else []
    print(f"  base url        : {env.get('ANTHROPIC_BASE_URL', '(unset)')}")
    print(f"  upstream        : {env.get('KOREAN_GUARD_UPSTREAM', '(unset)')}")
    print(f"  port {port} open  : {listening(port)}")
    print(f"  service file    : {'present' if service_installed() else 'absent'}")
    print(f"  pending findings: {len(flags)}")
    log = os.path.join(d, "proxy.log")
    if os.path.exists(log):
        tail = open(log, encoding="utf-8", errors="replace").read().splitlines()[-4:]
        for l in tail:
            print(f"    log: {l}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("detect", "render-service", "install-service", "dry-run", "wire",
                 "uninstall", "status"):
        p = sub.add_parser(name)
        p.add_argument("--root", default="")
        p.add_argument("--port", type=int, default=0)
        p.add_argument("--upstream", default=DEFAULT_UPSTREAM)
        p.add_argument("--flag-dir", default="")
        p.add_argument("--out", default="")
    a = ap.parse_args()
    if a.cmd in ("render-service", "install-service", "dry-run", "wire") and not a.port:
        sys.exit("--port is required for this step")
    return {
        "detect": cmd_detect, "render-service": cmd_render_service,
        "install-service": cmd_install_service, "dry-run": cmd_dry_run,
        "wire": cmd_wire, "uninstall": cmd_uninstall, "status": cmd_status,
    }[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
