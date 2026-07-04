"""hostagent — the single native GPU host agent (018 US2, FR-168..178).

One torch-free process owns all GPU access: in-process race-free admission (`admission.py`),
every engine a supervised child process behind one tenant lifecycle with per-engine adapters
(`lifecycle.py`), transactional evict→free→load swap (`swap.py`), a durable job journal
(`journal.py`), and a directly-scraped metrics endpoint (`metrics.py` + `main.py`).

Stdlib-only, like the supervisors it replaces (Principle III; the optional `pynvml` accelerates
GPU reads when present — research R1). The strangler migration is complete (T364): the legacy
daemons and the cross-process lockfile are gone, so the agent's in-process admission is the sole
authority for the one-tenant invariant.
"""
