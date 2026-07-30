#!/usr/bin/env python3
"""Authoring benchmark: measure Hangul corruption on the path where it actually happens.

Why not the copy benchmark (run.py): a verbatim-copy task produced 0 corruption in 92,256
round-tripped Hangul, and its re-read metric saturated at 100% in both arms. Two reasons
it cannot work — the Korean is already in context as tokens, so the model echoes rather
than re-encodes; and it was delivered through Write (flat string, local file), whereas the
failures concentrate in AskUserQuestion and MCP writes (nested args, long Korean bodies).

This harness fixes both:
  * the model **authors** fresh Korean instead of copying
  * delivery goes through a mock MCP tool with nested array-of-object arguments
    (sink_server.py), matching the shape where corruption is observed

Ground truth without a Korean dictionary: each task requires a fixed list of terms to
appear verbatim. Those terms are known, so any same-length one-syllable-off variant of a
required term in the delivered text is a corruption candidate. Surrounding prose has no
ground truth and is not scored — the metric is per required-term instance, not per 10k.

Arms
  baseline : KOREAN_GUARD_DISABLE=1        session note suppressed
  plugin   : CLAUDE_KOREAN_GUARD_FILE=...  session note fires

Metrics
  term_miss     required term instances that did not arrive verbatim
  ed1           same-length one-syllable-off variants of a required term (candidates,
                each kept with context for hand confirmation)
  fetched       did the agent call fetch_document after submitting? (verification
                behaviour on an external-write path; the prompt never hints at it)
"""
import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
SPECS = os.path.join(HERE, "authoring")
PAYLOAD = os.path.join(PLUGIN_ROOT, "reference", "hangul-toolarg-corruption.md")
HANGUL = re.compile(r"[가-힣]")

PROMPT_DOC = (
    "아래 주제로 한국어 기술 설명 문서를 작성해서 `submit_document` 도구로 제출해라.\n\n"
    "주제: {topic}\n\n"
    "조건:\n"
    "- 본문 합계 {min_chars}자 이상\n"
    "- 섹션 4개 이상으로 나눈다\n"
    "- 아래 용어를 각각 최소 {each}회, 표기를 바꾸지 않고 그대로 사용한다:\n"
    "{terms}\n"
)

PROMPT_ASK = (
    "아래 상황에서 담당자에게 물어볼 결정 사항을 정리해 `ask_decision` 도구로 물어라.\n\n"
    "상황: {topic}\n\n"
    "조건:\n"
    "- 질문 {n_questions}개\n"
    "- 질문마다 선택지 {n_options}개, 각 선택지의 label과 description을 모두 채운다\n"
    "- description은 한 문장 이상으로 충실히 쓴다\n"
    "- 아래 용어를 각각 최소 {each}회, 표기를 바꾸지 않고 그대로 사용한다:\n"
    "{terms}\n"
)


def build_prompt(spec):
    terms = "\n".join("  - " + t for t in spec["terms"])
    each = spec.get("each_at_least", 2)
    if spec.get("tool") == "ask_decision":
        return PROMPT_ASK.format(topic=spec["topic"], each=each, terms=terms,
                                 n_questions=spec.get("n_questions", 4),
                                 n_options=spec.get("n_options", 4))
    return PROMPT_DOC.format(topic=spec["topic"], each=each, terms=terms,
                             min_chars=spec.get("min_chars", 1200))


def delivered_strings(spec, payload):
    """Every Korean-carrying string that arrived, per tool shape."""
    out = []
    if not isinstance(payload, dict):
        return out
    if spec.get("tool") == "ask_decision":
        for q in payload.get("questions") or []:
            if not isinstance(q, dict):
                continue
            for k in ("question", "header"):
                if isinstance(q.get(k), str):
                    out.append(q[k])
            for o in q.get("options") or []:
                if isinstance(o, dict):
                    for k in ("label", "description"):
                        if isinstance(o.get(k), str):
                            out.append(o[k])
        return out
    if isinstance(payload.get("title"), str):
        out.append(payload["title"])
    for s in payload.get("sections") or []:
        if isinstance(s, dict):
            for k in ("heading", "body"):
                if isinstance(s.get(k), str):
                    out.append(s[k])
    return out


def arm_env(arm, sink):
    env = dict(os.environ)
    env.pop("KOREAN_GUARD_DISABLE", None)
    env.pop("CLAUDE_KOREAN_GUARD_FILE", None)
    env["KTG_SINK_FILE"] = sink
    if arm == "baseline":
        env["KOREAN_GUARD_DISABLE"] = "1"
    else:
        env["CLAUDE_KOREAN_GUARD_FILE"] = PAYLOAD
    return env


def ed1_variants(term, text):
    """Same-length, exactly-one-syllable-different occurrences of `term` in `text`."""
    out, n = [], len(term)
    for i in range(len(text) - n + 1):
        w = text[i:i + n]
        if w == term:
            continue
        diff = [k for k in range(n) if w[k] != term[k]]
        if len(diff) != 1:
            continue
        # require a word-ish boundary on both sides, so a window cutting through a
        # longer word does not register
        before = text[i - 1] if i else ""
        after = text[i + n] if i + n < len(text) else ""
        if HANGUL.match(before or "") or HANGUL.match(after or ""):
            continue
        k = diff[0]
        # only count when both sides are Hangul: filters punctuation/latin coincidences
        if not (HANGUL.match(w[k]) and HANGUL.match(term[k])):
            continue
        out.append({"term": term, "got": w,
                    "ctx": text[max(0, i - 20):i + n + 20]})
    return out


def score(spec, doc):
    text = "\n".join(delivered_strings(spec, doc))
    need = spec.get("each_at_least", 2)
    per_term, miss, flags = {}, 0, []
    for t in spec["terms"]:
        c = text.count(t)
        per_term[t] = c
        if c < need:
            miss += need - c
            # Only look for variants of terms that came up SHORT. Scanning every term
            # drowns in false positives: legitimate distinct words sit one syllable
            # apart (디코더/인코더, 추출/호출) and windows straddle word boundaries
            # (단계는 -> "계는" against 계층). If a term arrived the required number of
            # times, a near-neighbour elsewhere is a different word, not corruption.
            flags += ed1_variants(t, text)
    if spec.get("tool") == "ask_decision":
        units = len(doc.get("questions") or []) if isinstance(doc, dict) else 0
    else:
        units = len(doc.get("sections") or []) if isinstance(doc, dict) else 0
    return {"authored_hangul": len(HANGUL.findall(text)),
            "authored_chars": len(text),
            "sections": units,
            "term_counts": per_term,
            "term_required": len(spec["terms"]) * need,
            "term_miss": miss,
            "ed1": flags}


def run_trial(arm, spec, model, keep=False):
    work = tempfile.mkdtemp(prefix=f"ktga-{arm}-")
    sink = os.path.join(work, "sink.jsonl")
    mcp_cfg = os.path.join(work, "mcp.json")
    with open(mcp_cfg, "w", encoding="utf-8") as f:
        json.dump({"mcpServers": {"sink": {
            "command": sys.executable,
            "args": [os.path.join(HERE, "sink_server.py")],
            "env": {"KTG_SINK_FILE": sink}}}}, f)

    prompt = build_prompt(spec)
    cmd = ["claude", "-p", prompt, "--permission-mode", "bypassPermissions",
           "--mcp-config", mcp_cfg, "--strict-mcp-config"]
    if model:
        cmd += ["--model", model]

    rec = {"arm": arm, "spec": spec["id"], "model": model or "(default)"}
    try:
        p = subprocess.run(cmd, cwd=work, env=arm_env(arm, sink), capture_output=True,
                           text=True, stdin=subprocess.DEVNULL, timeout=1200)
        rec["exit"] = p.returncode
        rec["final_text"] = (p.stdout or "")[-3000:]
        rec["stderr"] = (p.stderr or "")[-600:]
    except subprocess.TimeoutExpired:
        rec["exit"] = -1
        rec["final_text"] = ""
        rec["error"] = "timeout"

    want = spec.get("tool", "submit_document")
    doc, fetched, submits = None, 0, 0
    if os.path.exists(sink):
        for line in open(sink, encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("kind") == want:
                doc = r["payload"]
                submits += 1
            elif r.get("kind") == "fetch_document":
                fetched += 1
    rec["submitted"] = doc is not None
    rec["submits"] = submits
    rec["fetched"] = fetched > 0
    if doc is not None:
        rec.update(score(spec, doc))
    rec["guard_injected"] = guard_fired(work)
    if not keep:
        shutil.rmtree(work, ignore_errors=True)
    return rec


def guard_fired(work):
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or \
        os.path.join(os.path.expanduser("~"), ".claude")
    root = os.path.join(cfg, "projects")
    if not os.path.isdir(root):
        return 0
    tag = os.path.basename(work)
    hits = [os.path.join(root, d) for d in os.listdir(root) if tag in d]
    n = 0
    for proj in hits:
        for dp, _, fns in os.walk(proj):
            for fn in fns:
                if not fn.endswith(".jsonl"):
                    continue
                for raw in open(os.path.join(dp, fn), encoding="utf-8",
                                errors="replace"):
                    if "모든 도구 호출 인자" in raw:
                        n += 1
        shutil.rmtree(proj, ignore_errors=True)
    return n


def summarize(rows):
    print(f"\n{'arm':9s} {'n':>3} {'sub':>4} {'hangul':>8} {'miss':>5} "
          f"{'ed1':>4} {'fetch':>6} {'guard':>6}")
    print("-" * 52)
    for arm in sorted({r["arm"] for r in rows}):
        rs = [r for r in rows if r["arm"] == arm and r.get("submitted")]
        allr = [r for r in rows if r["arm"] == arm]
        if not rs:
            print(f"{arm:9s} {len(allr):3d}    0   (no submissions)")
            continue
        print(f"{arm:9s} {len(allr):3d} {len(rs):4d} "
              f"{sum(r['authored_hangul'] for r in rs):8,d} "
              f"{sum(r['term_miss'] for r in rs):5d} "
              f"{sum(len(r['ed1']) for r in rs):4d} "
              f"{sum(1 for r in rs if r['fetched'])/len(rs):5.0%} "
              f"{sum(1 for r in rs if r['guard_injected'])/len(rs):5.0%}")
    print("\n  miss  = 요구 용어가 그대로 도착하지 않은 인스턴스 수")
    print("  ed1   = 요구 용어의 한 음절만 다른 동일 길이 변형 (손상 후보, 수동 확인 대상)")
    print("  fetch = 제출 후 fetch_document로 되읽었는가 (프롬프트에 힌트 없음)")
    flags = [(r["arm"], f) for r in rows for f in r.get("ed1", [])]
    if flags:
        print(f"\n  손상 후보 {len(flags)}건:")
        for arm, f in flags[:20]:
            print(f"    [{arm}] {f['term']} -> {f['got']}   …{f['ctx']}…")
    else:
        print("\n  손상 후보 0건")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=4)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--arms", default="baseline,plugin")
    ap.add_argument("--model", default="", help="pin the model, e.g. opus-5 / sonnet-5")
    ap.add_argument("--specs", default="")
    ap.add_argument("--out", default=os.path.join(HERE, "author_results.jsonl"))
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    names = [x for x in a.specs.split(",") if x] or \
        sorted(f for f in os.listdir(SPECS) if f.endswith(".json"))
    specs = [json.load(open(os.path.join(SPECS, n), encoding="utf-8")) for n in names]
    arms = [x for x in a.arms.split(",") if x]
    jobs = [(arm, s) for arm in arms for s in specs for _ in range(a.trials)]
    total = len(jobs)
    print(f"{total} trials, {a.jobs} at a time, model={a.model or '(default)'}\n")

    rows, done, lock = [], [0], threading.Lock()
    fh = open(a.out, "w", encoding="utf-8")

    def work(job):
        arm, spec = job
        r = run_trial(arm, spec, a.model, keep=a.keep)
        with lock:
            done[0] += 1
            rows.append(r)
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            fh.flush()
            print(f"[{done[0]}/{total}] {arm:8s} {r['spec']:4s} "
                  f"sub={str(r.get('submitted')):5s} "
                  f"hangul={r.get('authored_hangul', 0):5d} "
                  f"miss={r.get('term_miss', '-'):>3} "
                  f"ed1={len(r.get('ed1', [])):2d} "
                  f"fetch={str(r.get('fetched')):5s} "
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
