"""Audited bridge to the gateway's in-process cores from a native (non-gateway) process (T362.1).

The training flows (`scoring`, `hpo`, `batch_infer`, `shadow_replay`) reuse the gateway's evaluation /
batch / shadow logic. Those cores live in `gateway/app` and are tightly coupled to the gateway's
registry (evaluation<->registry, shadow->quality->registry), so relocating them wholesale is a
whole-gateway restructure (deferred — see specs/018 tasks T362 / handoff §FR-176). This module
centralizes the ONE `sys.path` insertion that makes `app.*` importable outside the gateway image,
replacing the four duplicated `sys.path.insert(0, gateway/)` blocks the training seams each carried
(FR-176). Ask for a core by name; the injection happens once, here — easy to audit, and easy to delete
when the cores finally move.

Loaded from `platformlib`, which is on `sys.path` in every runtime that reaches here: the gateway
image (COPYs platformlib) and the host agent that runs jobs (`PYTHONPATH=$REPO`). The pre-018 trainer
that lacked it is retired (T362).
"""
import os
import sys

_GATEWAY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gateway")


def _ensure_gateway_on_path() -> None:
    """Put `gateway/` on sys.path so `import app.*` resolves — idempotent; the one place it happens."""
    if _GATEWAY not in sys.path:
        sys.path.insert(0, _GATEWAY)


def evaluation():
    """The gateway's 011 eval harness (metric registry, benchmark loader, `_log_eval`)."""
    _ensure_gateway_on_path()
    from app import evaluation as ev
    return ev


def batch():
    """The gateway's 014 batch scoring core (`score_dataset` + the content-addressed write)."""
    _ensure_gateway_on_path()
    from app import batch as b
    return b


def shadow():
    """The gateway's 016 shadow-replay orchestration (window resolve → verdict → persist)."""
    _ensure_gateway_on_path()
    from app import shadow as s
    return s
