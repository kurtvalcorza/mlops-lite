#!/usr/bin/env bash
# Disk-frugality report (T040): where the bytes are, across the two storage planes.
# Container images sit on the Windows C: drive (the tight constraint); model/training artifacts
# sit in WSL (ample). Run from WSL: bash scripts/disk_report.sh
set -uo pipefail

echo "=== WSL filesystem ==="
df -h / "$HOME" 2>/dev/null | sort -u

echo
echo "=== Model / training artifacts (WSL) ==="
for d in "$HOME/models" "$HOME/.cache/huggingface" "$HOME/mlops-train" "$HOME/llama.cpp"; do
  [ -e "$d" ] && du -sh "$d" 2>/dev/null
done

echo
echo "=== Docker disk usage (images/volumes — these land on Windows C:) ==="
docker system df 2>/dev/null || echo "(docker not reachable from here; run 'docker system df' on the host)"

echo
echo "Tips: 'docker image prune -f' after rebuilds; cap the local model zoo; keep large fp16"
echo "weights in WSL (~), not in container images; relocate Docker data-root if C: is tight."
