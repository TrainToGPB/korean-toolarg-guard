# bench — attempts to measure this defect, and what they showed

Two harnesses live here. Both are **negative results**, and they are kept because the
negative results are informative: they narrow down where the defect lives and show why a
clean A/B of the plugin is hard to construct.

Nothing here demonstrates that the plugin reduces corruption. If you are looking for
evidence that the *problem* is real, that is the retrospective audit written up in
[this comment](https://github.com/anthropics/claude-code/issues/79339#issuecomment-5125788936),
not this directory.

## run.py — verbatim copy through `Write`

Hand the agent a fixed Korean passage, have it write the passage verbatim to a file, diff
the delivered file against the source character by character. Any mismatch is corruption by
construction, so no Korean dictionary is needed and there are no false positives.

```bash
./run.py --selftest          # validates the scorer, spends no API budget
./run.py --trials 8 --jobs 8
```

**Result: 0 corruption in 92,256 round-tripped Hangul** (96 trials, 6 passages, 96/96 exact
match, both arms).

The copy task does not reproduce the defect at all. Two plausible reasons, and they point
the same way:

- The source Korean is already in context as tokens, so the model echoes tokens instead of
  re-encoding them. The escape spelling is a *generation* choice, and echoing sidesteps it.
- Delivery went through `Write` — a flat string argument to a local file. The corruption is
  reported in `AskUserQuestion` (nested `questions[].options[].label`) and in MCP writes
  (long page bodies), i.e. different argument shapes on a different path.

Also note the re-read metric saturated: **100% in both arms** where it was measurable. An
instruction like "reproduce this without changing a single character" induces verification
by itself, so the metric has no discriminating power on this task. (The 88%/85% printed by
an earlier version of the summary was an artifact — those trials were ones whose transcript
the harness failed to locate, with `write_calls=0`.)

## run_author.py — fresh authoring through a nested MCP argument

Fixes both suspected problems: the model **authors** new Korean rather than copying, and it
delivers through `sink_server.py`, a mock stdio MCP server whose `submit_document(title,
sections[{heading, body}])` reproduces the nested array-of-objects shape where failures
concentrate. A `fetch_document` tool makes read-back observable on an external-write path,
and the prompt never hints at verifying.

Ground truth without a dictionary: each task requires a fixed list of terms to appear
verbatim a minimum number of times. Those terms are known, so a term arriving fewer times
than required is a signal.

```bash
./run_author.py --trials 4 --jobs 6 --model claude-opus-5
```

**Status: the pipeline works, and no corruption has been detected yet.** In smoke trials
both arms submitted successfully, authored ~1,300–1,500 Hangul, and delivered every
required term the required number of times (`term_miss=0`).

### The near-miss detector needed fixing, and the fix matters

The first version flagged every same-length one-syllable-off window anywhere in the text.
It produced 26–30 "candidates" per trial, **all of them false positives**:

- legitimate distinct words sit one syllable apart — `디코더`/`인코더`, `추출`/`호출`
- windows straddle word boundaries — `단계는` matches `계는` against the term `계층`

It now only looks for variants of terms that came up **short**, and requires a word-ish
boundary on both sides. Under that rule the same smoke output yields 0 candidates. This is
the same false-positive wall that makes an automated detector impractical in general:
distinguishing `파라밌터` from `파라미터` needs Korean lexical knowledge, not string
distance.

## Why a clean A/B is hard: the baseline is contaminated

Arms are separated by an env switch on the guard (`KOREAN_GUARD_DISABLE=1` vs
`CLAUDE_KOREAN_GUARD_FILE=…`), because isolating by `CLAUDE_CONFIG_DIR` does not work —
credentials live in the OS keychain, so a cloned config directory reports "Not logged in"
no matter what is copied into it.

But the env switch only suppresses the *hook*. A user-level `~/.claude/CLAUDE.md` is loaded
regardless, and if it carries a condensed pointer about this defect, **the baseline arm sees
it too**. That is visible in the smoke output: a baseline trial volunteered "제출 후 되읽어
확인했고 … 한글 손상은 발견되지 않았습니다" without being asked to verify anything.

So any measured plugin-vs-baseline difference here is a *marginal* effect over that pointer,
not an effect against a blank slate — and on the verification metric both arms sit at the
ceiling. A genuinely clean baseline needs the pointer temporarily neutralised, which means
touching live config that other sessions share; do that deliberately, not as a side effect
of running a benchmark. `--bare` is not a substitute: it disables hooks too, so it removes
the plugin arm's mechanism along with the contamination.

## Honest summary

| question | answer |
|---|---|
| Is the corruption real? | Yes — hand-verified in real authored output (see the linked comment) |
| Does a copy task reproduce it? | No. 0 in 92,256 Hangul |
| Does short-form authoring over a nested MCP argument reproduce it? | Not so far |
| Does the plugin reduce corruption? | **Not demonstrated.** No corruption observed in either arm, so there is nothing to reduce in these conditions |
| Does the plugin increase verification? | **Not demonstrated.** Both arms verify at ceiling, and the baseline is contaminated by the user-level pointer |

What would move this forward, in rough order of value: much longer authored payloads (the
one hand-verified real case was ~6,000 Hangul in a single body string, roughly 4× these
trials); a genuinely clean baseline; and, for scoring free prose rather than seeded terms, a
morphological analyser with a false-positive rate calibrated against known-clean Korean.
