# bench — known-answer round-trip measurement

Measuring this defect looks impossible at first: the corruption produces *valid* Hangul, so
spotting it normally requires Korean lexical knowledge. This harness sidesteps that. It hands
the agent a **fixed** source passage, asks it to write that passage verbatim into a file (so
the Korean has to travel through a tool argument), then diffs the delivered file against the
source character by character.

Any mismatch is corruption **by construction**. No dictionary, no heuristics, no false
positives — which is what makes a number defensible.

```bash
./run.py --selftest                  # validate the scorer, spends no API budget
./run.py --trials 3                  # pilot: 4 passages x 3 x 2 arms = 24 trials
./run.py --trials 20 --out full.jsonl
```

## Arms

| arm | what runs |
|---|---|
| `baseline` | isolated config dir, no plugin |
| `plugin` | same isolated config dir, `--plugin-dir <repo>` |

Each trial gets a throwaway `CLAUDE_CONFIG_DIR` and a throwaway cwd, so the operator's own
hooks, MCP servers, memory and `CLAUDE.md` cannot leak into either arm. Without that
isolation the baseline is contaminated the moment the guard is installed globally.

## Metrics

| metric | tests | notes |
|---|---|---|
| `corrupt/10k` | **prevention** | mismatched Hangul syllables per 10,000 round-tripped. Continuous, so it has far more power per trial than a pass/fail count |
| `inc` | prevention | fraction of trials with ≥1 mismatch |
| `reread` | **behaviour** | did the agent `Read` the file back after writing it? Objective, and likely the largest effect |
| `final_text` | detection | saved verbatim so "did it actually report the damage?" can be judged by hand |

Two distinct hypotheses, worth keeping separate:

- **H1 (prevention)** — the session note says to write raw UTF-8 rather than escapes. If the
  model complies even partly, `corrupt/10k` should drop. This may well come out at zero
  effect; that is a legitimate result and should be published as one.
- **H2 (behaviour/detection)** — the plugin's actual claim. `reread` should rise sharply. This
  needs far fewer trials to establish.

## Sizing

The corpus round-trips ~2,880 Hangul per full pass (4 passages, 690–770 each).

- **Pilot (24 trials)** is enough to validate the pipeline and estimate the base rate. Cheap:
  roughly 2–6k tokens per trial, so well under 150k total.
- **Full run** should be sized from the pilot's base rate. The efficient lever is *longer
  passages*, not more trials: corruption scales with payload length, so doubling passage
  length roughly doubles exposure per API call, while another trial costs a whole extra call.
  ~1,500–2,000 Hangul per passage is a reasonable ceiling before the model starts summarising
  instead of copying.

## Honest limitations

- **It measures a copy task, not authoring.** Being told "reproduce this verbatim" may itself
  change whether the model reaches for escape spelling. The retrospective figure from real
  authoring (13 corruptions in ~6,000 Hangul of an MCP page body) is the sanity check that the
  proxy is in the right ballpark, but the two are not the same task.
- **A copy task is the only way to get ground truth.** Authoring has no reference to diff
  against, which is exactly why the defect went unmeasured for so long.
- **Whitespace and punctuation drift are ignored** on purpose; only Hangul mismatches count.
- **H1 may show nothing.** The corruption happens below the level a prompt can reach, so a
  null prevention result would not invalidate the plugin — it only confirms that detection,
  not prevention, is where the value is.
- Source passages do not need to be *correct* Korean, only *fixed*, since the metric is
  round-trip fidelity against stored bytes. They are still kept clean so the model is never
  tempted to "fix" them, which would register as a spurious mismatch.

Results are written as JSONL with per-trial mismatch examples (including codepoint names),
so any published number can be traced back to the exact syllables that changed.
