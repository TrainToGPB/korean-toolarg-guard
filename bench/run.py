#!/usr/bin/env python3
"""Known-answer round-trip benchmark for Hangul corruption in tool arguments.

The measurement problem is that corruption produces *valid* Hangul, so detecting it
normally needs Korean lexical knowledge. This harness sidesteps that entirely: it hands
the agent a fixed source passage, asks it to write that passage verbatim into a file (so
the Korean must travel through a tool argument), then diffs the delivered file against
the source character by character. Any mismatch is corruption by construction — no
dictionary, no heuristics, no false positives.

Note the source passages do not need to be *correct* Korean, only *fixed*: the metric is
round-trip fidelity against the stored bytes. (They are still kept clean so the agent is
never tempted to "fix" them, which would register as a spurious mismatch.)

Arms
  baseline : isolated config dir, no plugin
  plugin   : same isolated config dir + --plugin-dir <plugin>

Isolation: each trial runs with CLAUDE_CONFIG_DIR pointed at a throwaway directory and
cwd set to a throwaway directory, so the operator's own hooks, MCP servers, memory and
CLAUDE.md cannot leak into either arm.

Metrics
  corrupt_chars / 10k Hangul   prevention signal (does the session note reduce escaping?)
  trials with >=1 mismatch     per-trial incidence
  reread_rate                  did the agent Read the file back after writing it?
  final_text                   saved verbatim for manual judging of "did it report it?"

Usage
  ./run.py --trials 8 --arms baseline,plugin --out results.jsonl
  ./run.py --selftest          # validates the scorer, spends no API budget
"""
import argparse
import concurrent.futures
import difflib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import unicodedata

HANGUL = re.compile(r"[가-힣]")
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
CORPUS = os.path.join(HERE, "corpus")

PROMPT = (
    "아래 <원문> 블록 안의 한국어 텍스트를 파일 `{target}` 에 **한 글자도 바꾸지 않고 그대로** 저장해라.\n"
    "요약·번역·교정·재구성 금지. 오탈자처럼 보이는 것도 고치지 말고 원문 그대로 둔다.\n"
    "블록 표시(<원문>, </원문>)는 파일에 넣지 않는다.\n\n"
    "<원문>\n{body}\n</원문>\n"
)


# ── how the arms are separated ───────────────────────────────────────────────
# Isolating by CLAUDE_CONFIG_DIR does not work: credentials live in the macOS
# keychain, not in the config directory, so a cloned config reports "Not logged
# in" no matter what is copied across. Arms are therefore separated in the real
# environment by an env switch on the guard itself:
#
#   baseline : KOREAN_GUARD_DISABLE=1        -> the session note never fires
#   plugin   : CLAUDE_KOREAN_GUARD_FILE=...  -> the note fires with the plugin's
#                                               own payload
#
# Known confound, disclosed rather than hidden: if the operator has the condensed
# pointer in their user-level CLAUDE.md, both arms see it, so the plugin arm is
# measured as a *marginal* effect over that pointer, not against a blank slate.
# The pooled corruption rate across both arms is unaffected by this and is the
# more robust number of the two.
PAYLOAD = os.path.join(PLUGIN_ROOT, "reference", "hangul-toolarg-corruption.md")


def arm_env(arm):
    env = dict(os.environ)
    env.pop("KOREAN_GUARD_DISABLE", None)
    env.pop("CLAUDE_KOREAN_GUARD_FILE", None)
    if arm == "baseline":
        env["KOREAN_GUARD_DISABLE"] = "1"
    else:
        env["CLAUDE_KOREAN_GUARD_FILE"] = PAYLOAD
    return env


# ── scoring ──────────────────────────────────────────────────────────────────
def diff_hangul(src, got):
    """Character-level alignment; returns (mismatched_hangul, examples)."""
    sm = difflib.SequenceMatcher(None, src, got, autojunk=False)
    bad, ex = 0, []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        a, b = src[i1:i2], got[j1:j2]
        n = len(HANGUL.findall(a)) + len(HANGUL.findall(b))
        if n == 0:
            continue                      # whitespace / punctuation drift: ignore
        bad += max(len(HANGUL.findall(a)), len(HANGUL.findall(b)))
        if len(ex) < 12:
            ex.append({"op": tag,
                       "src": a[:24], "got": b[:24],
                       "ctx": src[max(0, i1 - 18):i2 + 18],
                       "codepoints": [f"U+{ord(c):04X} {unicodedata.name(c, '?')}"
                                      for c in b[:3]]})
    return bad, ex


def score(src, got):
    total = len(HANGUL.findall(src))
    bad, ex = diff_hangul(src, got)
    return {"hangul_total": total,
            "corrupt_chars": bad,
            "per_10k": round(bad / total * 10000, 1) if total else 0.0,
            "exact_match": src.strip() == got.strip(),
            "examples": ex}


# ── one trial ────────────────────────────────────────────────────────────────
def run_trial(arm, passage_path, keep=False):
    src = open(passage_path, encoding="utf-8").read().strip()
    work = tempfile.mkdtemp(prefix=f"ktg-{arm}-")
    target = os.path.join(work, "out.md")
    prompt = PROMPT.format(target=target, body=src)

    cmd = ["claude", "-p", prompt, "--permission-mode", "bypassPermissions"]
    env = arm_env(arm)
    rec = {"arm": arm, "passage": os.path.basename(passage_path), "workdir": work}
    try:
        p = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=900)
        rec["exit"] = p.returncode
        rec["final_text"] = (p.stdout or "")[-4000:]
        rec["stderr"] = (p.stderr or "")[-800:]
    except subprocess.TimeoutExpired:
        rec["exit"] = -1
        rec["final_text"] = ""
        rec["error"] = "timeout"

    if os.path.exists(target):
        got = open(target, encoding="utf-8", errors="replace").read()
        rec.update(score(src, got))
        rec["wrote_file"] = True
    else:
        rec["wrote_file"] = False

    proj_dir, info = inspect_transcript(work, target)
    rec.update(info)
    if not keep:
        shutil.rmtree(work, ignore_errors=True)
        if proj_dir:                    # don't leave a transcript dir per trial behind
            shutil.rmtree(proj_dir, ignore_errors=True)
        rec.pop("workdir", None)
    return rec


def inspect_transcript(work, target):
    """Did a Read of the target follow the Write? Also count guard injections.

    Trials run in the real config dir, so locate this trial's transcript by the
    unique temp cwd name that Claude Code slugifies into the project dir name.
    """
    out = {"reread": False, "write_calls": 0, "guard_injected": 0, "nudges": 0}
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or \
        os.path.join(os.path.expanduser("~"), ".claude")
    proj_root = os.path.join(cfg, "projects")
    tag = os.path.basename(work)
    if not os.path.isdir(proj_root):
        return None, out
    hits = [os.path.join(proj_root, d) for d in os.listdir(proj_root) if tag in d]
    if not hits:
        return None, out
    proj = hits[0]
    files = [os.path.join(dp, fn) for dp, _, fns in os.walk(proj)
             for fn in fns if fn.endswith(".jsonl")]
    saw_write = False
    for path in files:
        for raw in open(path, encoding="utf-8", errors="replace"):
            # Marker taken from the payload text itself. Matching the script name
            # would false-positive on the hook command line, which the transcript
            # records on every trial whether the note fired or not.
            if "모든 도구 호출 인자" in raw:
                out["guard_injected"] += 1
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            c = (rec.get("message") or {}).get("content")
            if not isinstance(c, list):
                continue
            for b in c:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name, inp = b.get("name"), b.get("input") or {}
                if name in ("Write", "Edit") and inp.get("file_path") == target:
                    out["write_calls"] += 1
                    saw_write = True
                if name == "Read" and inp.get("file_path") == target and saw_write:
                    out["reread"] = True
    return proj, out


# ── reporting ────────────────────────────────────────────────────────────────
def summarize(rows):
    print(f"\n{'arm':10s} {'n':>3} {'wrote':>6} {'exact':>6} {'inc':>6} "
          f"{'corrupt/10k':>12} {'reread':>7} {'guard':>6}")
    print("-" * 66)
    for arm in sorted({r["arm"] for r in rows}):
        rs = [r for r in rows if r["arm"] == arm and r.get("wrote_file")]
        if not rs:
            print(f"{arm:10s}   0   (no successful trials)")
            continue
        inc = sum(1 for r in rs if r["corrupt_chars"] > 0) / len(rs)
        rates = [r["per_10k"] for r in rs]
        print(f"{arm:10s} {len(rs):3d} {len(rs):6d} "
              f"{sum(1 for r in rs if r['exact_match']):6d} "
              f"{inc:6.0%} {statistics.mean(rates):12.1f} "
              f"{sum(1 for r in rs if r['reread'])/len(rs):7.0%} "
              f"{sum(1 for r in rs if r['guard_injected'])/len(rs):6.0%}")
    tot = sum(r["hangul_total"] for r in rows if r.get("wrote_file"))
    print(f"\n  총 왕복 한글: {tot:,}자")
    print("  corrupt/10k = 한글 1만자당 불일치 음절 (예방 지표)")
    print("  reread      = Write 후 같은 파일을 Read 했는가 (습관 지표)")
    print("  inc         = 1자 이상 깨진 시행 비율")
    ex = [(r["arm"], e) for r in rows for e in r.get("examples", [])]
    if ex:
        print(f"\n  불일치 예시 ({len(ex)}건 중 최대 10):")
        for arm, e in ex[:10]:
            print(f"    [{arm}] {e['op']:7s} {e['src']!r} -> {e['got']!r}")


# ── selftest: validates the scorer without spending API budget ───────────────
def selftest():
    src = "파라미터를 추출해 계층별 평균을 갱신하고, 요청한 산출물을 반환하겠습니다."
    cases = [
        ("identical", src, 0),
        ("one syllable wrong",
         src.replace("파라미터", "파라밌터"), 1),
        ("three wrong",
         src.replace("파라미터", "파라밌터").replace("계층", "계습").replace("평균", "평군"), 5),
        ("whitespace only", src.replace(", ", ",  "), 0),
        ("deletion", src.replace("갱신하고, ", ""), 4),
    ]
    ok = True
    print("scorer selftest")
    for name, got, expect_min in cases:
        s = score(src, got)
        good = (s["corrupt_chars"] == 0) if expect_min == 0 else (s["corrupt_chars"] >= 1)
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {name:22s} "
              f"corrupt={s['corrupt_chars']:2d} per10k={s['per_10k']:7.1f} "
              f"exact={s['exact_match']}")
    passages = sorted(f for f in os.listdir(CORPUS) if f.endswith(".md")) \
        if os.path.isdir(CORPUS) else []
    print(f"\ncorpus: {len(passages)} passages")
    for f in passages:
        t = open(os.path.join(CORPUS, f), encoding="utf-8").read()
        print(f"  {f:24s} {len(HANGUL.findall(t)):5d} hangul  {len(t):5d} chars")
    print("\n" + ("ALL PASS" if ok and passages else "PROBLEM"))
    return 0 if (ok and passages) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=4, help="trials per arm per passage")
    ap.add_argument("--jobs", type=int, default=6, help="trials to run concurrently")
    ap.add_argument("--arms", default="baseline,plugin")
    ap.add_argument("--passages", default="", help="comma-separated filenames; default all")
    ap.add_argument("--out", default=os.path.join(HERE, "results.jsonl"))
    ap.add_argument("--keep", action="store_true", help="keep trial workdirs")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    names = [x for x in a.passages.split(",") if x] or \
        sorted(f for f in os.listdir(CORPUS) if f.endswith(".md"))
    arms = [x for x in a.arms.split(",") if x]
    jobs = [(arm, os.path.join(CORPUS, name))
            for arm in arms for name in names for _ in range(a.trials)]
    total = len(jobs)
    print(f"{total} trials, {a.jobs} at a time\n")

    rows, done, lock = [], [0], threading.Lock()
    fh = open(a.out, "w", encoding="utf-8")

    def work(job):
        arm, path = job
        r = run_trial(arm, path, keep=a.keep)
        with lock:
            done[0] += 1
            rows.append(r)
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            fh.flush()
            print(f"[{done[0]}/{total}] {arm:8s} {r['passage']:14s} "
                  f"wrote={str(r.get('wrote_file')):5s} "
                  f"corrupt={str(r.get('corrupt_chars', '-')):>3s} "
                  f"reread={str(r.get('reread')):5s} "
                  f"guard={r.get('guard_injected')}", flush=True)
        return r

    with concurrent.futures.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        list(ex.map(work, jobs))
    fh.close()
    summarize(rows)
    print(f"\nraw -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
