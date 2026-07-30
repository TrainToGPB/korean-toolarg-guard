#!/usr/bin/env bash
# Run the authoring A/B with a genuinely clean baseline.
#
# The env switch (KOREAN_GUARD_DISABLE=1) only silences the hook. A user-level
# ~/.claude/CLAUDE.md is loaded regardless, so if it carries a pointer about this
# defect the baseline arm sees it too and both arms verify. This wrapper removes
# that section for the duration of the run and restores it on every exit path,
# including Ctrl-C and errors.
#
# Side effect worth knowing: other Claude Code sessions on this account share the
# file, so they lose the pointer during the window. Keep runs short.
#
#   ./clean_ab.sh --trials 6 --jobs 8 --model claude-opus-5
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
MD="$CFG/CLAUDE.md"
BAK="$MD.ab-backup-$$"
MARK="# 도구 인자의 한글 손상 (모든 tool args)"

restore() {
  if [ -f "$BAK" ]; then
    mv -f "$BAK" "$MD"
    if grep -qF "$MARK" "$MD"; then
      echo "CLAUDE.md restored ✓"
    else
      echo "CLAUDE.md RESTORE VERIFY FAILED — check $MD" >&2
    fi
  fi
}
trap restore EXIT INT TERM

if [ ! -f "$MD" ]; then
  echo "no $MD — baseline already clean"
else
  cp -p "$MD" "$BAK"
  python3 - "$MD" "$MARK" <<'PY'
import sys
path, mark = sys.argv[1], sys.argv[2]
lines = open(path, encoding="utf-8").read().split("\n")
out, drop = [], False
for ln in lines:
    if ln.strip() == mark:
        drop = True
        continue
    # a following top-level heading ends the section
    if drop and ln.startswith("# ") and ln.strip() != mark:
        drop = False
    if not drop:
        out.append(ln)
open(path, "w", encoding="utf-8").write("\n".join(out))
print(f"CLAUDE.md: guard section removed for the run ({len(lines)} -> {len(out)} lines)")
PY
  if grep -qF "$MARK" "$MD"; then
    echo "FATAL: section still present, aborting" >&2
    exit 1
  fi
  grep -c '되읽' "$MD" | sed 's/^/  잔존 되읽기 언급: /' || true
fi

echo
"$HERE/run_author.py" "$@"
