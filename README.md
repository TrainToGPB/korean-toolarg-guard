# korean-toolarg-guard

Claude Code sometimes spells Korean inside **tool-call arguments** as `\uXXXX` Unicode
escapes instead of emitting the characters directly. That is a bad trade in both directions:
each syllable becomes several hex tokens, and one wrong digit lands on a *different but
perfectly valid* Hangul syllable — so the JSON stays well-formed, the file stays valid UTF-8,
no error is raised, and the words are simply wrong.

This plugin catches the escaping itself and refuses the call before it runs.

한국어 요약은 [아래](#한국어)에 있습니다.

## The failure

```
intended   파라미터를 추출한다
delivered  파라밌터를 추출한다        ← 미 → 밌, one hex digit
```

More observed pairs: `계층 → 계습`, `갱신 → 갱슱`, `평균 → 평군`, `요청 → 요정`,
`산출물 → 산증물`, `통째 → 통챠`. U+AC00–D7A3 is densely packed, so almost any single-digit
slip produces another legal syllable. One page carried 13 such corruptions in ~6,000 Hangul.

Decode correctness is not really the point, though. Escape spelling is worse even when it
works: capacity goes into transliterating hex rather than composing content, the text cannot
be read back as meaning while it is being produced, and token count inflates several-fold.

## Why it cannot be caught from inside Claude Code

The harness decodes escapes when it parses the tool-call JSON. Everything downstream of that
parse — the transcript, the model's own context, a hook's `tool_input` — holds only the
decoded characters. Escaped and raw are **indistinguishable** there.

So the obvious ideas do not work, and it is worth saying why so nobody rebuilds them:

- **A "no unicode escapes in arguments" hook cannot work.** Hooks sit below the decode;
  there is nothing left to inspect.
- **Round-tripping proves nothing.** Fetching the stored content back and comparing it to
  context always agrees, because the intended original was never present in a verifiable
  form. That checks transport and storage, not spelling.
- **Instructing "write raw UTF-8" is unverifiable.** Neither the model nor a hook can
  confirm compliance after the fact.

The network is the one vantage point upstream of the parse.

## How this works

Two pieces, because neither can do the job alone:

| | sees the escapes | can stop the call |
|---|---|---|
| `proxy/tee_proxy.py` | **yes** — reads the raw wire format | no |
| `hooks/escape_guard.py` | no — `tool_input` arrives decoded | **yes** — `PreToolUse` deny |

They are joined by `tool_use_id`. In a streaming response, tool arguments arrive as
`content_block_delta` events carrying `delta.input_json_delta.partial_json`. That field is
itself a JSON string, so a backslash the model emitted is doubled on the wire; decoding each
event **once** undoes the transport's escaping and leaves exactly what the model produced.
The proxy counts escaped versus raw Hangul, drops a finding keyed by `tool_use_id`, and the
hook turns it into a refusal:

```
Escape-encoded Korean detected — 412 Hangul syllables in this call's arguments were
written as \uXXXX Unicode escapes. Write tool arguments as raw text: put the Korean
characters in directly, never as \uXXXX. … Re-emit the same content without escapes
and call the tool again.
```

The refusal lands **before** the tool executes, so nothing is written and nothing has to be
cleaned up. The proxy writes its finding before forwarding the chunk, so the flag always
beats the CLI to `PreToolUse` — no race.

`hooks/session_note.py` additionally states the failure mode once per session, on the first
prompt, so the habit is present even with no proxy running.

## Install

```shell
/plugin marketplace add TrainToGPB/korean-toolarg-guard
/plugin install korean-toolarg-guard@hangul-tools
/reload-plugins
```

Requires `python3` on `PATH`. Standard library only, no dependencies.

Installed on its own, the plugin injects the session note and nothing else: with no proxy
running the flag directory stays empty and `escape_guard` exits immediately on every call.

### Turning on detection

```bash
python3 proxy/selftest.py          # offline check, no API budget

python3 proxy/tee_proxy.py --upstream https://api.anthropic.com/v1 --port 8099 --verbose
```

Then point a **separate** shell at it — leave your real settings alone:

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8099/v1 claude
```

No TLS interception is involved: the CLI speaks plain HTTP to localhost and the proxy opens
its own TLS connection upstream, so no certificate has to be trusted. Authentication headers
pass through untouched and are never logged.

If an `env` block in your `settings.json` sets `ANTHROPIC_BASE_URL`, it wins over the shell
variable. Pass a copy with `--settings` instead of editing the original.

## Configuration

| Variable | Default | Effect |
|---|---:|---|
| `KOREAN_GUARD_FLAG_DIR` | `~/.claude/.state/escape-guard` | where the proxy drops findings |
| `KOREAN_GUARD_ESCAPE_MIN` | `1` | escaped Hangul syllables needed to refuse |
| `KOREAN_GUARD_MAX_DENY` | `2` | refusals per session before giving up and passing the call through with a warning |
| `KOREAN_GUARD_DISABLE` | unset | `1` disables both hooks for the shell |
| `KOREAN_GUARD_FORCE_FLAG` | unset | `1` flags every Korean call regardless of escaping — for testing the refusal path only |

The denial cap matters: a retry arrives with a fresh `tool_use_id`, so without a per-session
ceiling a model that deterministically re-escapes would loop.

## What has been verified, and what has not

**The detection is proven.** `proxy/selftest.py` covers eight cases including a server that
escapes all non-ASCII on the wire, a stream split mid-escape, both malformed-escape shapes
seen in the wild, and LaTeX (`\\underbrace`) not false-positiving.

**The refusal path is proven end to end.** With `KOREAN_GUARD_FORCE_FLAG=1`, a live session
was refused twice, the model read the reason and re-emitted, and the third call passed at the
cap — flag written, hook fired, tool never executed.

**How often real traffic escapes is largely unmeasured.** In `claude-opus-5`, 11,450 Hangul
across `Write` and a nested MCP argument came through with **zero** escapes, primed and
unprimed alike. On that model the guard may rarely fire. Corruption *has* been hand-verified
in older transcripts under `claude-opus-4-8` and `claude-opus-5`, so the behaviour is
presumably intermittent or model-dependent; the proxy is what makes that measurable at all.

**It cannot tell a right escape from a wrong one.** `미` and `밌` are both perfectly
well-formed; nothing on the wire says which one was meant. Catching *that* needs Korean
lexical knowledge, which is why the session note asks for a read rather than a diff.

## Upstream

An open defect in Claude Code, not something a plugin can fix:

- [anthropics/claude-code#79339](https://github.com/anthropics/claude-code/issues/79339) —
  pinpoints the mechanism; labelled `has repro`
- [anthropics/claude-code#69522](https://github.com/anthropics/claude-code/issues/69522) —
  the broader parse-failure reports, on Windows and macOS, in Korean and Traditional Chinese

Both cover the **loud** branch: the escape is malformed, JSON parsing fails, the call is
rejected with a visible error. The silent branch — well-formed escapes with wrong digits —
raises nothing, which is why it needed measuring instead of reporting.
[Evidence and figures](https://github.com/anthropics/claude-code/issues/79339#issuecomment-5125788936).

## License

MIT

---

## 한국어

Claude Code가 **도구 호출 인자**의 한글을 `\uXXXX` 유니코드 이스케이프로 쓰는 경우가 있습니다.
음절마다 16진수 4자리를 맞혀야 하는데 한 자리만 틀려도 **다른 적법한 한글 음절**이 나옵니다
(`파라미터` → `파라밌터`, `계층` → `계습`). JSON도 정상, 파일도 정상 UTF-8, 오류도 없이 단어만
틀립니다. 디코딩이 맞았는지는 부차적입니다 — 이스케이프로 쓰는 순간 토큰이 몇 배로 불고, 모델이
자기 출력을 의미로 되읽지 못해 생성 품질도 떨어집니다.

**Claude Code 안에서는 이걸 볼 수 없습니다.** 훅이 받는 `tool_input`은 이미 디코딩된 결과라
이스케이프와 raw가 구분되지 않습니다. 그래서 "인자에 유니코드 금지" 훅도, 저장본과 대조하는
왕복 검증도 원리상 무효입니다. 유일한 관측점이 네트워크입니다.

그래서 두 조각으로 나눕니다. **프록시**가 와이어에서 이스케이프를 세고 `tool_use_id`로 플래그를
남기면, **`PreToolUse` 훅**이 그걸 읽어 호출을 거부하고 "raw text로 다시 쓰라"는 이유를 모델에게
돌려줍니다. 거부는 도구 실행 **전에** 걸리므로 잘못된 내용이 외부에 남지 않습니다.

프록시를 켜지 않으면 플러그인은 세션 첫 프롬프트에 주의사항만 넣고 나머지는 완전히 무해합니다.
설정 파일의 `env`가 셸 환경변수를 덮으므로, 원본을 고치지 말고 사본을 `--settings`로 넘기십시오.
