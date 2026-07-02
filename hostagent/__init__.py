"""hostagent — the single native GPU host agent (018 US2, FR-168..178).

One torch-free process owns all GPU access: in-process race-free admission (`admission.py`),
every engine a supervised child process behind one tenant lifecycle with per-engine adapters
(`lifecycle.py`), transactional evict→free→load swap (`swap.py`), a durable job journal
(`journal.py`), and a directly-scraped metrics endpoint (`metrics.py` + `main.py`).

Stdlib-only, like the supervisors it replaces (Principle III; the optional `pynvml` accelerates
GPU reads when present — research R1). During the strangler migration the agent participates in
the legacy lockfile protocol (`serving/gpu_lease.py`) so the one-tenant invariant holds across
the boundary; the interop shim and the lockfile are deleted together at T364.
"""
