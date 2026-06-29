"""Per-modality HPO search spaces (012 US2, FR-115).

Each modality declares the **tunable knobs + ranges** Optuna samples from, so a study searches only
the hyperparameters that mean something for that flow (LoRA rank is meaningless for a vision
classifier). Defaults are **sensible, not pinned** — `sample()` takes per-study `overrides` so an
operator can narrow/replace a range (mirrors 010/011's configurable-defaults posture).

A space is a plain dict of `knob -> spec`, where `spec` is one of:
  ("categorical", [choices])             -> trial.suggest_categorical
  ("loguniform", low, high)              -> trial.suggest_float(..., log=True)
  ("uniform", low, high)                 -> trial.suggest_float
  ("int", low, high[, step])             -> trial.suggest_int(..., step=…)

`sample()` only touches the `trial` via the standard Optuna `suggest_*` API, so it is exercised with a
real Optuna trial in production and a tiny fake trial in tests — no GPU, no training. The sampled dict
is forwarded straight into the existing `finetune_flow` knobs (FR-111 reuse).
"""
from typing import Optional

# LLM (LoRA) + vision are the **committed** spaces (their fine-tune flows are served today).
SPACES = {
    "llm": {
        "lora_r": ("categorical", [8, 16, 32, 64]),
        "lora_alpha": ("categorical", [16, 32, 64]),
        "lr": ("loguniform", 1e-5, 5e-4),
        "steps": ("int", 10, 60, 10),
    },
    "vision": {
        "lr": ("loguniform", 1e-5, 1e-2),
        "epochs": ("int", 1, 5),
        "unfreeze_epochs": ("categorical", [0, 1, 2]),  # "freeze-depth" — how many epochs to unfreeze
    },
}

# ASR / embeddings carried as **guidance stubs** — declared so the shape is visible, but their
# fine-tune paths only partially expose these knobs; wired when 010's ASR/embeddings tuning matures.
STUB_SPACES = {
    "asr": {
        "lr": ("loguniform", 1e-5, 5e-4),
        "epochs": ("int", 1, 5),
        "lora_r": ("categorical", [8, 16, 32]),
    },
    "embeddings": {
        "lr": ("loguniform", 1e-5, 5e-4),
        "epochs": ("int", 1, 4),
        "warmup_ratio": ("uniform", 0.0, 0.2),
    },
}

COMMITTED = set(SPACES)            # modalities with a wired, tunable fine-tune path today
KNOWN = COMMITTED | set(STUB_SPACES)


class SearchSpaceError(Exception):
    """No search space for the requested modality (or a malformed override)."""


def space_for(modality: str, overrides: Optional[dict] = None) -> dict:
    """The resolved search space for `modality` (committed spaces first, then stubs), with any
    per-study `overrides` merged in (an override replaces a knob's spec, or adds a new knob)."""
    base = SPACES.get(modality) or STUB_SPACES.get(modality)
    if base is None:
        raise SearchSpaceError(
            f"no HPO search space for modality {modality!r} (known: {sorted(KNOWN)})")
    return {**base, **(overrides or {})}


def sample(modality: str, trial, overrides: Optional[dict] = None) -> dict:
    """Sample one hyperparameter set for `modality` from `trial` (an Optuna Trial). Returns a dict of
    knob -> value ready to forward into `finetune_flow`."""
    out = {}
    for knob, spec in space_for(modality, overrides).items():
        kind = spec[0]
        if kind == "categorical":
            out[knob] = trial.suggest_categorical(knob, list(spec[1]))
        elif kind == "loguniform":
            out[knob] = trial.suggest_float(knob, spec[1], spec[2], log=True)
        elif kind == "uniform":
            out[knob] = trial.suggest_float(knob, spec[1], spec[2])
        elif kind == "int":
            step = spec[3] if len(spec) > 3 else 1
            out[knob] = trial.suggest_int(knob, spec[1], spec[2], step=step)
        else:
            raise SearchSpaceError(f"unknown spec kind {kind!r} for knob {knob!r}")
    return out
