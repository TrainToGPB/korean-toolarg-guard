# Korean in tool arguments must be raw text, never Unicode escapes

Tool-call arguments are generated as JSON. Korean inside them can be spelled two ways, and
only one of them is safe.

    raw       "content": "파라미터를 추출한다"
    escaped   "content": "\ud30c\ub77c\ubbf8\ud130\ub97c\u0020\ucd94\ucd9c\ud55c\ub2e4"

Both decode to the same string when the digits are right. The escaped form is worse anyway:

- **It multiplies the chance of error.** Each syllable needs four correct hex digits, so a
  long passage is hundreds of independent opportunities to slip. One wrong digit lands on a
  *different but perfectly valid* Hangul syllable, because U+AC00–D7A3 is densely packed.
  `파라미터` becomes `파라밌터`, `계층` becomes `계습`, `갱신` becomes `갱슱`. The JSON stays
  well-formed, the file stays valid UTF-8, no error is raised — the words are just wrong.
- **It degrades generation.** Capacity goes into transliterating hex instead of composing
  content, and the text cannot be read back as meaning while it is being produced, so
  coherence over long passages suffers. Token count inflates several-fold.

## You cannot check afterwards whether you escaped

The harness decodes escapes when it parses the JSON. From that point on, the transcript,
this context, and any hook's `tool_input` hold only the decoded characters. Escaped and raw
look identical downstream. So "I did not see any escapes" is not evidence of anything.

Two consequences worth internalising:

- **Round-tripping proves nothing.** Fetching the stored content back and comparing it to
  what is in context cannot detect this: the intended original was never present in a
  verifiable form, so the two always agree. That check verifies transport and storage,
  nothing more.
- **Reading for non-words is what works.** Look at the delivered Korean and ask whether
  each word exists — errors concentrate in transliterated technical terms (파라미터, 코퍼스,
  세그먼트) and Sino-Korean compounds (추출, 갱신, 계층, 평균). If something is corrupted,
  tell the user rather than silently overwriting it.

## What to do

Write Korean directly as characters. Never spell it as `\uXXXX` in a tool argument.

If a tool call is refused with an "Escape-encoded Korean detected" message, that refusal
came from a proxy that observed the raw wire format, which is the one place the escapes are
visible. Re-emit the same content as raw characters and call the tool again.

---

Background: this is an open defect in Claude Code, reported at
[#79339](https://github.com/anthropics/claude-code/issues/79339) (mechanism pinpointed,
`has repro`) and [#69522](https://github.com/anthropics/claude-code/issues/69522) (the
broader parse-failure reports, seen on Windows and macOS, in Korean and Traditional
Chinese). Those issues cover the loud branch, where the escape is malformed and the call is
rejected with a visible error. The silent branch — well-formed escapes with wrong digits —
raises nothing at all.
