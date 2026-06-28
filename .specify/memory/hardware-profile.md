# Hardware Profile

The **single machine-specific file** for this platform. Every resource constraint in the
constitution, spec, and plan is expressed relative to the parameters below — so to target a
different machine, edit this table and nothing else.

## Active profile

| Parameter | Value | Notes |
|---|---|---|
| `GPU_NAME` | NVIDIA GeForce RTX 5070 Ti Laptop GPU | single GPU |
| `VRAM_GB` | 12 | hard ceiling for one live model |
| `CUDA` | 13.3 | driver/runtime |
| `CPU` | Intel Core Ultra 9 275HX | 24 cores |
| `RAM_GB` | 31 | total system memory |
| `FREE_DISK_GB` | ~50 | the scarcest resource — budget carefully |
| `HOST_OS` | Windows 11 + WSL2 | |
| `CONTAINER_ENGINE` | Docker (Rancher Desktop) | any Compose engine with NVIDIA passthrough works |

## Derived budgets (referenced by the specs)

- **Live models in VRAM**: exactly **1 at a time**, each sized to fit `VRAM_GB`.
- **Model size cap**: pick models whose resident footprint ≤ ~`VRAM_GB − 1` (headroom).
- **Idle infra RAM**: ≤ ~3 GB (well within `RAM_GB`).
- **Disk**: ~15 GB for models + ~10 GB for images within `FREE_DISK_GB`; prune aggressively;
  relocate the container data-root if `FREE_DISK_GB` is tight.

## Retargeting

To run on different hardware, replace the **Active profile** values. If `VRAM_GB` changes, the
model size cap and the one-model-in-VRAM rule scale automatically; if `FREE_DISK_GB` changes,
adjust the disk budget. No edits to spec/plan/tasks are required.
