# Spike — Hardened GPU-sandbox feasibility on the WSL2 host (R7 release gate)

**Date**: 2026-07-19 · **Gate for**: FR-026 / P2 arbitrary-tenant job execution · **Result**: **INFEASIBLE on
WSL2 as-is** (confirmed at device level).

## Question
Can arbitrary tenant-submitted job code run inside a hardened sandbox (gVisor `runsc`+nvproxy, or Kata
Containers) **with GPU access** on this Windows 11 + WSL2 host — the isolated-kernel/VM boundary FR-026
mandates?

## Method
Probed a GPU-working distro (`mlperf`, Ubuntu 24.04, kernel `6.18.x-microsoft-standard-WSL2`, RTX 5070 Ti) for
the GPU device model and available runtimes (`scratchpad/wsl_sandbox_probe.sh` + an `lspci`/driver-node check).

## Evidence
```
nvidia-smi -L         → GPU 0: RTX 5070 Ti (works)
/dev/dxg              → present  (WSL paravirtual GPU-PV device)
/dev/nvidia*          → ABSENT   (no /dev/nvidiactl, /dev/nvidia-uvm, /dev/nvidia0)
lspci | grep nvidia   → NO NVIDIA PCI device (GPU is GPU-PV; nothing to VFIO-bind)
/dev/vfio             → present but useless without an assignable PCI GPU
CUDA path             → /usr/lib/wsl/lib/libcuda.so + /dev/dxg  (paravirtualized)
runsc / kata-runtime  → not installed ; nvidia-ctk → not installed
docker                → Rancher Desktop (moby); normal containers share the WSL2 kernel
```

## Why this is decisive (not just "not installed")
- **gVisor `nvproxy`** intercepts ioctls on the **real NVIDIA character devices** (`/dev/nvidiactl`,
  `/dev/nvidia-uvm`, `/dev/nvidia#`). On WSL2 those **do not exist** — CUDA goes through `/dev/dxg` (DirectX
  GPU kernel) via `libdxcore`. gVisor has **no dxg proxy**. → cannot work here.
- **Kata GPU** requires **VFIO passthrough of a real PCI GPU** into the micro-VM. WSL2's GPU is already
  **paravirtualized** (GPU-PV) into the WSL utility VM; there is **no PCI GPU function** to assign. → cannot
  work here.
- Installing the runtimes would not change the device model; the blocker is architectural (paravirtualized
  GPU), not a missing package.

## Consequence for FR-026 / P2
The "gVisor/Kata-class isolation, always" requirement for **arbitrary tenant code** is **not satisfiable on
the current WSL2 host**. Per R7, the sound options are:

1. **Signed broker-owned recipes (RECOMMENDED for the WSL host)** — P2 accepts only **vetted, parameterized
   job recipes the broker owns** (e.g., the 010 fine-tune / batch specs), **not** arbitrary tenant containers.
   No strong sandbox is required because no untrusted code runs. Ships on WSL now; **no new runtime → no
   constitution amendment needed.**
2. **Native-Linux GPU host** — move the host agent to bare-metal/dual-boot Linux (real `/dev/nvidia*`), where
   gVisor `nvproxy` or Kata VFIO is validated; then arbitrary-code jobs become possible. Large infra change;
   deferred.
3. **Defer P2 arbitrary jobs** entirely.

## Recommendation
Adopt **option 1** for this feature on the WSL host: **P2 = signed broker-owned recipes only.** Re-scope
FR-026 accordingly (arbitrary tenant code is out of scope on WSL; it requires the native-Linux path, tracked
as future work). This unblocks a *useful* P2 (users submit fine-tunes/batches from the vetted recipe set) with
**correct, honest isolation semantics**, and removes the new-runtime amendment dependency.

## DECISION (owner, 2026-07-19): **Option 2 — native-Linux GPU host.**
Keep **arbitrary-tenant jobs** in scope, with the required gVisor/Kata GPU-isolated sandbox, but **run the GPU
host on native Linux** (bare-metal / dual-boot; real `/dev/nvidia*`) where the sandbox is validatable. This
makes **P2 cross-cutting infra work**, gated on **three prerequisites**, none of which block P1/P3/P4 (those
stay on the current WSL host):
1. **Native-Linux GPU host migration** — the host agent + serving/training run on Linux with the NVIDIA
   driver exposing real char devices; a re-run of this spike on that host MUST pass (gVisor `nvproxy` **or**
   Kata VFIO validated end-to-end with a CUDA container).
2. **New-runtime constitution amendment** — gVisor/Kata is still a new runtime (Dev-Workflow clause) even on
   Linux; amend before shipping.
3. **P2 tasks are authored but marked BLOCKED** until (1)+(2) land; P1/P3/P4 proceed on WSL now.

Rationale: preserves the full "submit any job to the shared GPU" vision without pretending WSL can isolate it;
accepts an OS-level dependency as the honest cost of arbitrary-code isolation.
