#!/usr/bin/env bash
# WSL helper for down_all (002 US3, T054): stop the supervisor and all native daemons, leaving NO
# GPU orphans. SIGTERM the supervisor first (it terminates its children gracefully), then sweep any
# stragglers, then confirm the GPU has no leftover compute processes (VRAM released).
set -uo pipefail

# kill_match <cmdline-substring> <signal>: signal every process whose cmdline contains the substring.
# /proc scan (not pkill) avoids the self-match / pattern hazards seen through the WSL interop.
kill_match() {
  local needle="$1" sig="${2:-TERM}" hit=1 d cmd pid
  for d in /proc/[0-9]*; do
    cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
    case "$cmd" in
      *"$needle"*)
        pid="${d##*/}"
        echo "  kill -$sig $pid : $cmd"
        kill "-$sig" "$pid" 2>/dev/null
        hit=0
        ;;
    esac
  done
  return $hit
}

echo "[down] SIGTERM supervisor (graceful child shutdown) ..."
kill_match "supervisor/supervise.py" TERM || echo "  (no supervisor running)"
sleep 5

echo "[down] sweeping any leftover daemon processes ..."
# 018 T358: the GPU host agent is a supervised default now — sweep it (its llama-server child is
# caught by the GPU-compute sweep below) so a dead/stale supervisor or a restart never orphans it.
kill_match "hostagent/main.py" KILL || true
kill_match "hostagent/run.sh" KILL || true
# Keep sweeping the RETIRED llama supervisor too (Codex round 8, 018): a manually started or
# pre-upgrade serving/llama/supervisor.py — not a child of the current supervisor — would otherwise
# survive teardown holding :8081/VRAM. The file is gone from HEAD; the match is a migration backstop.
kill_match "serving/llama/supervisor.py" KILL || true
kill_match "training/trainer.py" KILL || true
kill_match "bentoml serve" KILL || true
kill_match "VisionClassifier" KILL || true
# Operator console (003 US1): run.sh -> npm -> next start. The Next server is a non-GPU localhost
# process (no VRAM), but sweep its tree so down_all leaves nothing bound on :3000.
kill_match "ui/run.sh" KILL || true
kill_match "next start" KILL || true
kill_match "next-server" KILL || true
sleep 2

# Authoritative VRAM release: kill ANY remaining GPU compute process by PID (catches llama-server,
# the verify-script llama-cli, or any GPU holdout) — name-matching alone misses processes nvidia-smi
# reports as "[Not Found]" under WSL. On this single-GPU platform, teardown clears the GPU.
echo "[down] releasing GPU (killing any remaining compute-app PIDs) ..."
if command -v nvidia-smi >/dev/null 2>&1; then
  for p in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null); do
    p="${p//[[:space:]]/}"
    [ -n "$p" ] || continue
    echo "  kill -9 $p (GPU compute app)"
    kill -9 "$p" 2>/dev/null || true
  done
  sleep 2
  apps=$(nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null)
  if [ -z "$apps" ]; then
    echo "  none remaining — VRAM released, no GPU orphans"
  else
    echo "$apps" | sed 's/^/  LEFTOVER: /'
    echo "[down] WARNING: GPU compute processes remain" >&2
  fi
else
  echo "  nvidia-smi not available"
fi
