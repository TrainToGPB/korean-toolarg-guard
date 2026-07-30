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

## Measured result (48 trials, `claude-opus-5`, 179,738 authored Hangul)

Run with `clean_ab.sh`, which removes the user-level `CLAUDE.md` pointer for the duration so
the baseline is genuinely uninformed, and restores it on every exit path.

### Verification behaviour: a large effect

| task | baseline | plugin |
|---|---|---|
| `a5` long document (~5,000 Hangul) | 1/8 | 8/8 |
| `a6` paper-summary document (~5,000 Hangul) | 1/8 | 8/8 |
| **document writes combined** | **2/16 (12%)** | **16/16 (100%)** |
| `a7` `ask_decision` (nested options, no document) | 0/8 | 0/8 |

Fisher exact, two-tailed, on the combined document writes: **p = 5.1 × 10⁻⁷**.

The `a7` row is a validity check, not a failure. `ask_decision` submits no document, so there
is nothing for `fetch_document` to return — and the plugin arm correctly does not call it.
The effect is targeted at writes that produced something re-readable, not blind tool-spam.

The hook fired in 21/24 plugin trials and 0/24 baseline trials. The three plugin trials
showing `guard=0` all still verified, so those are almost certainly transcript-location
misses in the harness rather than hook failures.

### Corruption: still zero, and that is the honest headline for H1

0 confirmed corruptions in 179,738 authored Hangul, in both arms.

- The two `term_miss` events were compliance, not corruption: `레이턴시` and `디코더` each
  appeared once where the task asked for twice.
- All four near-miss flags were `디코더 → 인코더`, the known false positive — two real words
  one syllable apart, where the model simply used `인코더` more often.

So the plugin's effect on **corruption** remains undemonstrated, because corruption did not
reproduce even at 180k Hangul on a nested MCP argument. That sits awkwardly against the
hand-verified real case (13 corruptions in ~6,000 Hangul of a Notion page body), and the gap
is worth naming rather than glossing: these trials are single-turn with a tiny tool schema,
whereas the real case came from a deep multi-turn session against Notion's much larger
schema with markdown-rich content. Whatever raises the rate in practice is not reproduced
here, so **no corruption base rate should be quoted from this harness.**

What can be quoted: the plugin changes verification behaviour from 12% to 100% on
external-write tasks, which is exactly the claim it makes.

## Why a clean baseline needs the pointer removed

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
| Does long-form authoring (~5,000 Hangul) over a nested MCP argument reproduce it? | No. 0 in 179,738 Hangul |
| Does the plugin reduce corruption? | **Not demonstrated** — corruption never reproduced, so there was nothing to reduce |
| Does the plugin increase verification? | **Yes.** 12% → 100% on external-write tasks, p = 5.1 × 10⁻⁷ |

What would move the corruption question forward: reproducing the conditions the real case had
but these trials do not — a large real tool schema, a deep multi-turn session, markdown-rich
content — and, for scoring free prose rather than seeded terms, a morphological analyser with
a false-positive rate calibrated against known-clean Korean.
