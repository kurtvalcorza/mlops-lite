"""platformlib — the shared contracts package (018 T343, FR-176).

The single definition of tenant identities, topology (ports/URLs/state dir), typed payloads, and
storage helpers used by BOTH runtimes: the gateway container (Dockerfile COPYs this package) and
the native WSL host (repo root on PYTHONPATH). Stdlib-only by contract (research R2) — a pydantic
version skew between the two runtimes must never be able to break the contracts themselves.

Import rules (contracts/platformlib.md): this package imports neither `gateway.app` nor
`hostagent` nor any ML runtime. Anything the two sides share moves HERE, never sideways.
"""
