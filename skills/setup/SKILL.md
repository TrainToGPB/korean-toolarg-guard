---
description: Set up, check, or remove escape detection for korean-toolarg-guard. Use when the user asks to enable/turn on/configure Korean escape detection or the guard proxy, wants to check whether it is running, or wants to undo it. Installs a supervised proxy service and points Claude Code at it.
---

# Setting up escape detection

Detection needs a proxy in the request path. That is the only place `\uXXXX`-escaped Korean is
visible — everything inside Claude Code sits below the JSON parse, where escaped and raw look
identical. Getting the proxy into the path means two things: a process that stays up, and
`ANTHROPIC_BASE_URL` pointing at it.

**The risk to state plainly, before doing anything:** once the base URL points at the proxy, a
proxy that is not listening means no API access at all — in this session and every other one on
this machine. So the order below verifies the proxy works *before* live configuration changes,
and every mutating step leaves a backup.

All mechanics live in `scripts/setup.py` next to this file. Run the steps and show the user the
real output; do not summarise a step as done without its output.

## 1. Look before touching anything

```bash
python3 "$SKILL_DIR/scripts/setup.py" detect
```

Read the JSON and tell the user, in a couple of lines: what endpoint they are on now
(`upstream_to_use`), the URL that would be written (`base_url_to_write`), whether they are
already wired up, which port will be used, and whether a service manager is available.

`base_url_to_write` mirrors the upstream's path and nothing else — `http://127.0.0.1:PORT` for a
bare host like `https://api.anthropic.com`, `http://127.0.0.1:PORT/v1/claude-code` for a gateway
with that path. The CLI appends `/v1/messages` on its own, so a `/v1` that is not in the upstream
must not appear here.

Then decide the mode:

- `service_manager` is `launchd` or `systemd` → **service mode** (recommended). The proxy is
  supervised, so it restarts if it dies mid-session.
- `service_manager` is `none` → **hook mode**. The plugin's `SessionStart` hook starts the proxy
  at the beginning of each session instead. Weaker: it cannot revive a proxy that dies
  mid-session. Skip step 3 and go straight to step 4.

If `already_wired` is true, this is a re-run: report the current state from `status` and ask what
they want changed rather than repeating the install.

## 2. Confirm with the user

Do not proceed without agreement. Show them:

- the port, and that the proxy will run as a supervised background service
- that `ANTHROPIC_BASE_URL` in their `settings.json` will change, with a backup written
- **the failure mode**: if the proxy stops and the base URL still points at it, API access stops
- how to undo: `python3 "$SKILL_DIR/scripts/setup.py" uninstall`

Use the `upstream_to_use` value from `detect` as the real endpoint. If they are behind a company
gateway, that value is their gateway — check it looks right with them, because the proxy forwards
there and a wrong value breaks every request.

## 3. Install the service, then prove it works

```bash
python3 "$SKILL_DIR/scripts/setup.py" install-service --port <PORT> --upstream <UPSTREAM>
python3 "$SKILL_DIR/scripts/setup.py" dry-run --port <PORT>
```

`dry-run` sends one real request through the proxy using a throwaway settings copy, so it proves
the path end to end while the user's live configuration is still untouched.

**If `dry-run` fails, stop.** Do not wire anything. Show `status` output and the tail of
`~/.claude/.state/escape-guard/proxy.log`, and work out why — a wrong upstream and a port
collision are the usual causes.

Two specific temptations to refuse, both of which have bricked a machine:

- **Do not wire a value you already know is wrong, planning to correct it afterwards.** The
  moment `settings.json` points somewhere that does not answer, you lose the API access you
  need to make the correction — the session dies between the two steps and leaves the user
  with no working CLI. Fix the value first, re-run `dry-run`, wire once.
- **Do not read a model error at face value here.** A base URL that 404s surfaces as
  *"There's an issue with the selected model … Run /model to pick a different model."*
  Nothing is wrong with the model, and `/model` cannot help. Check `base_url_to_write`
  against the upstream's path before believing anything the error says.

## 4. Wire it up

```bash
python3 "$SKILL_DIR/scripts/setup.py" wire --port <PORT> --upstream <UPSTREAM>
```

This backs up `settings.json`, sets `ANTHROPIC_BASE_URL` to the proxy, and records the real
endpoint in `KOREAN_GUARD_UPSTREAM`. It re-parses the file afterwards and fails loudly if the
edit broke it.

Tell the user the backup path and that **existing sessions keep their old endpoint** — the change
takes effect in new sessions.

## 5. Verify and hand over

```bash
python3 "$SKILL_DIR/scripts/setup.py" status
```

Then explain what they will now see:

- Korean written into a tool argument as escapes gets the call **refused before it runs**, with a
  message telling the model to re-emit raw text. Nothing is written, so nothing needs cleaning up.
- After two refusals in a session the guard stops refusing and lets the call through with a
  warning, so a model that keeps escaping cannot loop.
- Escaped Korean may simply never appear. Recent models mostly emit raw UTF-8; a quiet guard is a
  good outcome, not a broken one. `status` shows the finding count if they want to check.

## Checking later, and undoing

```bash
python3 "$SKILL_DIR/scripts/setup.py" status
python3 "$SKILL_DIR/scripts/setup.py" uninstall
```

`uninstall` stops and removes the service, restores the original endpoint, and removes the guard's
env keys — with a backup. Offer it whenever the user reports that requests started failing.

## When the CLI is already broken

The symptom is every prompt in every session answering *"There's an issue with the selected
model … Run /model to pick a different model."* If that started after wiring, the model is not
the problem: the base URL is, and `/model` will not fix it.

`uninstall` is the fix and needs no API access, so it still works from a dead session. Failing
that — a truncated `settings.json`, an interrupted run — the edit is small enough to do by hand,
and every `wire` left a `settings.json.ktg-backup-*` beside it:

```bash
python3 - <<'EOF'
import json
p = __import__("os").path.expanduser("~/.claude/settings.json")
d = json.load(open(p)); env = d.get("env", {})
for k in ("ANTHROPIC_BASE_URL", "KOREAN_GUARD_UPSTREAM", "KOREAN_GUARD_PORT",
          "KOREAN_GUARD_BASE_WAS_UNSET"):
    env.pop(k, None)
d["env"] = env
json.dump(d, open(p, "w"), ensure_ascii=False, indent=2)
EOF
```

That returns the machine to the CLI's default endpoint. Confirm with
`claude -p 'reply with exactly: ok'` before doing anything else, then stop the service
(`systemctl --user disable --now korean-toolarg-guard.service`, or `launchctl bootout
gui/$(id -u)/com.korean-toolarg-guard.proxy`) so nothing is left half-installed.

Only restore a whole backup file if you have checked what else it would revert — the user may
have changed `model` or other keys since it was written.

## Notes

- `render-service` prints the launchd plist or systemd unit without installing it, for a user who
  wants to read it first.
- The proxy log is `~/.claude/.state/escape-guard/proxy.log`. It records tool-input findings only:
  no request bodies, no headers, no credentials.
- Do not edit `settings.json` by hand in these steps; `wire` and `uninstall` handle the backups
  and validation.
