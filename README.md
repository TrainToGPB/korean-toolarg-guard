# korean-toolarg-guard

Korean text written into **tool-call arguments** can come out as *different but perfectly
valid* Hangul syllables. The JSON is well-formed, the file is valid UTF-8, no error is
raised — the words are just quietly wrong. This plugin makes that failure mode known at
the start of every session and flags Korean-heavy writes to external services so the
content actually gets re-read.

한국어 설명은 [아래](#한국어)에 있습니다.

## What goes wrong

| Intended | What actually got written |
|---|---|
| 파라미터 / 대비 / 채택 | `파라밌터` `대별` `채필` |
| 추출 / 맞춰 / 형태 | `추천` `맞춴` `형토` |
| 갱신 / 평균 / 계층 | `갱슱` `평군` `계습` |
| 요청 / 산출물 / 하겠습니다 | `요정` `산증물` `하게습니다` |
| 통째 / 긁어 | `통챠` `긍어` |

One observed page had **13 corruptions in 6,000 Hangul characters** — it gets worse the
longer the text. It is not tool-specific: it has been observed in `AskUserQuestion`
arguments and in Notion MCP page bodies, in both flat strings and deeply nested
array-of-object arguments. Occasionally a stray space is inserted mid-word, a Hangul
jamo filler (U+115F) appears, or a required field arrives empty.

### The cause: Unicode escapes with wrong hex digits

Tool arguments are generated as JSON, and Korean inside them gets written as `\uXXXX`
Unicode escapes. Emitting four correct hex digits per syllable is the fragile step: one
wrong digit yields a *different but perfectly valid* Hangul syllable, because
U+AC00–D7A3 is densely packed. That is why these errors do not look like errors. The
same failure has been reported on gpt-oss models.

**The trap: you cannot verify after the fact that escapes were used.** The harness
decodes escapes to characters when it parses the JSON, and only those characters come
back — into the transcript, into the model's own context, and into `tool_input` as a hook
receives it. So the absence of literal escapes downstream is *not* evidence that escapes
weren't used.

The positive evidence is the occasional escape that leaks out broken. One observed
argument arrived as `\u洞\uchㅊ\uc튼` (intended: 규칙) — Hangul and Han characters
sitting where hex digits belong. Strings containing U+115F (Hangul Choseong Filler) point
the same way: that codepoint cannot be typed, only mis-encoded.

It also explains the asymmetry: prose is emitted as plain text tokens and comes out
clean, while arguments travel through the JSON escape path.

Two consequences:

- **Instructing raw UTF-8 in arguments is directionally right, but unverifiable.**
  Neither the model nor a hook can confirm compliance, so it cannot be relied on alone.
- **A syntactic "no unicode escapes" hook cannot work.** Hooks sit *downstream* of the
  decode, so nothing is left to inspect. Automated detection would need a Korean
  dictionary or a morphological analyzer.

Generation is what degrades; recognition is fine. That asymmetry is the basis for this
plugin's approach: it does not try to prevent the corruption, it makes sure someone looks.

### Upstream issues

This is a known, open defect in Claude Code, not something a plugin can fix:

- [anthropics/claude-code#79339](https://github.com/anthropics/claude-code/issues/79339) —
  pinpoints the mechanism: the model glitches mid-escape and emits a backslash followed by
  a raw CJK character, making the whole input unparsable. Labelled `has repro`.
- [anthropics/claude-code#69522](https://github.com/anthropics/claude-code/issues/69522) —
  the broader report: long unicode-escaped tool arguments failing JSON parse. Corroborated
  on both Windows and macOS, and in Traditional Chinese as well as Korean.

Both were **open** as of July 2026, on `claude-opus-4-8` and `claude-opus-5` alike.

Note the difference in consequence. Those reports cover the **loud** branch: the escape is
invalid, JSON parsing fails, and the call is rejected with an error you can see and retry.
This plugin exists for the **silent** branch: the escape is well-formed but the hex digits
are wrong, so the JSON parses, the call succeeds, and the corrupted text simply lands. Same
cause, no error — which is why it needs a reading habit rather than an error handler.
Evidence and measurements: [this comment](https://github.com/anthropics/claude-code/issues/79339#issuecomment-5125788936).

## What this plugin does

**1. `UserPromptSubmit` — session note (once per session).** Injects
[`reference/hangul-toolarg-corruption.md`](reference/hangul-toolarg-corruption.md) as
context on the first prompt of each session, so the failure mode and the re-read habit
are known before any Korean is written. Silent on every subsequent prompt.

**2. `PreToolUse` — re-read nudge on Korean-heavy MCP writes.** Matched to `mcp__.*`
only. When a call's arguments carry more Korean than the threshold, a short note lands
next to the tool result saying how much Korean went out and to re-read it. Capped per
session so it cannot become noise.

Local `Write`/`Edit` are deliberately **not** nudged: you review those in diffs. The
dangerous case is a long Korean body published to a service where nobody looks again.

## Install

```shell
/plugin marketplace add TrainToGPB/korean-toolarg-guard
/plugin install korean-toolarg-guard@hangul-tools
/reload-plugins
```

Or try it without installing:

```bash
claude --plugin-dir ./korean-toolarg-guard
```

Requires `python3` on `PATH` (standard library only — no dependencies).

## Configuration

| Environment variable | Default | Effect |
|---|---:|---|
| `KOREAN_GUARD_MIN_HANGUL` | `150` | Minimum Hangul characters in a call's arguments before the nudge fires |
| `KOREAN_GUARD_MAX_NUDGES` | `6` | Per-session nudge ceiling. `0` disables the nudge entirely, leaving only the session note |

## What this plugin does not do

- **It does not detect corruption.** It cannot: telling `파라밌터` from `파라미터`
  requires Korean lexical knowledge, not a syntax check. It tells you *where to look*.
- **It does not fix anything.** Auto-correcting Korean would silently rewrite correct
  but unusual terms and proper nouns — worse than the original problem.
- **It does not prevent the corruption.** The damage happens below the level any prompt,
  instruction, or hook can reach. This is a smoke detector, not wiring repair.

Both hooks fail open: any error exits 0 with no output, so a broken guard can never
block a session.

## Evidence

Compiled in July 2026 from a hand-verified audit of one user's complete Claude Code
session logs (398 sessions, 267 MB). Corruption was confirmed in `AskUserQuestion` and
Notion MCP arguments; user-typed Korean in the same logs showed none of it. The
underlying defect appears to be in the decoding/serving layer, which is worth reporting
upstream — this plugin exists because the damage is silent in the meantime.

## License

MIT

---

## 한국어

**도구 호출 인자(tool arguments)에 넣은 한글이 적법한 다른 음절로 조용히 바뀌어 나갑니다.**
JSON도 완전하고 파일도 정상 UTF-8이라 오류가 나지 않고, 단어만 틀립니다. 관측된 한 사례는
한글 6,000자에 13곳이 깨져 있었고, 분량이 길수록 늘어납니다. `AskUserQuestion`과 Notion MCP
양쪽에서 확인됐으므로 특정 도구 문제가 아닙니다.

**원인은 JSON 인자를 유니코드 이스케이프로 쓰다 16진수가 틀리는 것입니다.** 한 자리만 어긋나도
U+AC00–D7A3 구간이 촘촘해서 다른 적법한 음절로 착지합니다. gpt-oss 계열에서도 같은 이슈가
보고된 이력이 있습니다. 함정은 **이스케이프로 썼다는 사실을 사후에 확인할 수 없다**는 점입니다 —
하니스가 파싱할 때 문자로 디코딩되어 트랜스크립트·모델 컨텍스트·훅의 `tool_input`에는 결과
문자만 돌아옵니다. 그래서 raw UTF-8로 쓰라는 지시는 방향은 맞아도 준수 검증이 불가능하고,
"인자에 유니코드 금지" 같은 구문 검사도 실효가 없습니다(훅은 디코드 하류).
두 실패 양상은 업스트림에 이미 보고돼 있고 **2026-07 기준 미해결**입니다 —
[#79339](https://github.com/anthropics/claude-code/issues/79339)(메커니즘 특정, `has repro`),
[#69522](https://github.com/anthropics/claude-code/issues/69522)(파싱 실패 전반). 다만 두 이슈는
이스케이프가 **무효**여서 호출이 거부되는 loud 분기이고, 이 플러그인이 다루는 것은 이스케이프가
**유효한데 16진수만 틀려** 조용히 통과하는 silent 분기입니다. 근거: [코멘트](https://github.com/anthropics/claude-code/issues/79339#issuecomment-5125788936).


**생성은 손상되지만 인식은 정상입니다.** 이 플러그인은 예방이 아니라 검출을 돕습니다.

1. **세션 노트** — 세션 첫 프롬프트에 이 실패 양상과 되읽기 습관을 컨텍스트에 넣습니다. 이후
   프롬프트에서는 침묵합니다.
2. **되읽기 알림** — MCP 쓰기 호출의 인자에 한글이 임계 이상 들어 있으면, 얼마나 나갔는지와
   되읽으라는 짧은 노트를 tool result 옆에 붙입니다. 세션당 횟수 상한이 있어 소음이 되지
   않습니다. 로컬 `Write`/`Edit`은 사용자가 diff로 보므로 일부러 제외했습니다.

한글을 피하거나 분량을 줄이는 우회는 권하지 않습니다 — 문제를 옮기는 것입니다. 한글로 쓰되
검증하는 쪽입니다. 두 훅 모두 오류 시 exit 0으로 조용히 통과하므로 세션을 막지 않습니다.
