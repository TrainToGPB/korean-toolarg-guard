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

Read the JSON and tell the user, in a couple of lines: what endpoint they are on now, whether
they are already wired up, which port will be used, and whether a service manager is available.

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

## Notes

- `render-service` prints the launchd plist or systemd unit without installing it, for a user who
  wants to read it first.
- The proxy log is `~/.claude/.state/escape-guard/proxy.log`. It records tool-input findings only:
  no request bodies, no headers, no credentials.
- Do not edit `settings.json` by hand in these steps; `wire` and `uninstall` handle the backups
  and validation.
