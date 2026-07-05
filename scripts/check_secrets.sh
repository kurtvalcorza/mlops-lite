#!/usr/bin/env bash
# Secret-leak guard (T061, supports SC-009): fail if a credential looks committed to the repo.
# Run locally or in CI. Scans git-tracked files (falls back to a working-tree scan if git is
# unavailable), excluding the git-ignored secret files, the documented template, and this tooling.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# High-signal patterns for a real, committed credential (not prose mentioning one):
#   - mll_<24+ alnum>  : a generated gateway API key
#   - =GK<24 hex>      : a Garage access-key id used as a VALUE (store-minted at bootstrap and
#                        recorded only into the git-ignored .env — never legitimately committed)
# (The retired store's shipped-default-credential pattern left with it at 020 T406: Garage has
# no shipped default — keys exist only after the store mints them.)
PATTERNS='(mll_[A-Za-z0-9]{24,}|=GK[0-9a-f]{24})'

# Never scan these: the git-ignored secret files (they legitimately hold secrets), the documented
# template (placeholders), this guard, and the secret generators (which produce keys at runtime).
EXCLUDE='(^|/)\.env$|(^|/)\.env\.example$|(^|/)secrets/|(^|/)check_secrets\.sh$|(^|/)gen_secrets\.(sh|ps1)$'

if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
  # 1. The secret files themselves must not be tracked.
  if git ls-files --error-unmatch .env >/dev/null 2>&1; then
    echo "[FAIL] .env is tracked by git — it must be git-ignored (contains secrets)." >&2
    exit 1
  fi
  mapfile -t FILES < <(git ls-files | grep -Ev "$EXCLUDE")
else
  echo "[warn] git not available — scanning the working tree instead." >&2
  mapfile -t FILES < <(find . -type f \
    -not -path './.git/*' -not -path '*/__pycache__/*' -not -path '*/.venv/*' \
    | sed 's|^\./||' | grep -Ev "$EXCLUDE")
fi

HITS=0
for f in "${FILES[@]}"; do
  [[ -f "$f" ]] || continue
  if grep -EnH "$PATTERNS" "$f" 2>/dev/null; then
    HITS=1
  fi
done

if [[ "$HITS" -ne 0 ]]; then
  echo "" >&2
  echo "[FAIL] possible committed secret(s) above — remove them and rotate (scripts/gen_secrets)." >&2
  exit 1
fi

echo "[OK] no committed credentials detected."
