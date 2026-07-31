# Hardware Profile

The **single machine-specific file** for this platform. Every resource constraint in the
constitution, spec, and plan is expressed relative to the parameters below — so to target a
different machine, edit this table and nothing else.

## Active profile

| Parameter | Value | Notes |
|---|---|---|
| `GPU_NAME` | NVIDIA GeForce RTX 5070 Ti Laptop GPU | single GPU |
| `VRAM_GB` | 12 | device total; the co-residency budget derives from this (see below) |
| `CUDA` | 13.3 | driver/runtime |
| `CPU` | Intel Core Ultra 9 275HX | 24 cores |
| `RAM_GB` | 31 | total system memory |
| `FREE_DISK_GB` | ~50 | the scarcest resource — budget carefully |
| `HOST_OS` | Windows 11 + WSL2 | |
| `CONTAINER_ENGINE` | Docker (Rancher Desktop) | any Compose engine with NVIDIA passthrough works |

## Derived budgets (referenced by the specs)

- **Live models in VRAM**: **bounded co-residency** within a VRAM budget (constitution **v1.6.1**,
  Principle II). Superseded the pre-1.6.0 "exactly 1 at a time" rule. Two bounds hold simultaneously:
  1. `Σ accounted residents + Σ outstanding reservations ≤ usable_capacity`
  2. each individual load ≤ `live_free − unmaterialized_reservations − safety_headroom`
- **Exclusive jobs remain exclusive**: a running job takes the whole GPU and is never preempted.
- **Model size cap**: pick models whose resident footprint ≤ ~`VRAM_GB − 1` (headroom).
- **Idle infra RAM**: ≤ ~3 GB (well within `RAM_GB`).
- **Disk**: ~15 GB for models + ~10 GB for images within `FREE_DISK_GB`; prune aggressively;
  relocate the container data-root if `FREE_DISK_GB` is tight.

## GPU admission tunables

Defaults for the coordinator's admission protocol (`contracts/admission-scheduler.md`). All are
machine-relative and belong here rather than in the spec.

| Tunable | Default | Meaning |
|---|---|---|
| `safety_reserve` | 1.0 GB | held back from `VRAM_GB`; `usable_capacity = min(configured_budget, VRAM_GB − safety_reserve)` |
| `safety_headroom` | 0.5 GB | slack required above an incoming load's estimate against live-free |
| `max_admission_attempts` | 3 | bounded reserve→evict→retry cycles before refusing `gpu_busy` |
| `drain_timeout` | 30 s | max wait for a victim's in-flight requests to finish before the eviction is abandoned |
| `job_drain_timeout` | 120 s | max wait for the serving set to empty before an exclusive job gives up its barrier |
| `admission_backoff` | 250 ms base, exponential, jittered, capped 5 s | backoff between admission attempts |

`safety_reserve` covers driver/context overhead and unaccounted external GPU consumers; on a shared
desktop GPU (this profile — WSL2 with a display attached) it should not be reduced below 1 GB.

## Retargeting

To run on different hardware, replace the **Active profile** values. If `VRAM_GB` changes, the
model size cap and the co-residency budget scale automatically; if `FREE_DISK_GB` changes,
adjust the disk budget. Retune `safety_reserve` if the GPU is dedicated (no display attached),
where a smaller reserve is safe. No edits to spec/plan/tasks are required.
