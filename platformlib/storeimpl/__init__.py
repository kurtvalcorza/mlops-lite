"""023 US7 (T543, FR-324/325) — internal relational-repository modules behind the `store` facade.

`platformlib.store` is the public entry point ~60 call sites import (`from platformlib import store`;
`store.log_prediction(...)`). Its relational repositories are being lifted, one cohesive concern at a
time, into modules under this package to shrink the store.py hotspot WITHOUT changing any caller: the
facade re-exports every name, and `tests/test_store_facade.py` pins the surface so nothing is dropped.

Shared primitives (the store errors + the epoch/jsonb seam helpers) live in `_base`; each repository
module imports only from `_base`, so there is no circular import back through the facade.
"""
