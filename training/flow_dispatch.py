"""Shared fine-tune dispatch (010; subprocess-isolation refactor).

Maps a `modality` + the `/train` request to the right flow call. Extracted from `trainer.py` so it is
imported by **both** the daemon (for the 400-unknown-modality guard) and `run_flow.py` (the subprocess
entry that actually runs the flow with a clean CUDA context — see trainer.py's `_worker`).

Heavy ML imports stay lazy *inside* each branch, so importing this module is cheap (the daemon never
pulls torch into its own process — that is exactly what the subprocess isolation buys us).
"""
import os

VALID_MODALITIES = ("llm", "vision", "embeddings", "asr")  # one daemon dispatches all four


def dispatch(modality: str, req: dict) -> dict:
    """Run the fine-tune flow for `modality`. All four share the dataset/output/seed/parent_version
    contract; per-modality knobs default to each flow's conservative VRAM-fitting defaults when the
    Runs form omits them (FR-098). Raises ValueError for an unknown modality."""
    common = dict(
        dataset_name=req["dataset_name"],
        dataset_version=req["dataset_version"],
        output_name=req["output_name"],
        seed=int(req.get("seed", 0)),
        parent_version=req.get("parent_version") or None,
    )
    base = req.get("base_model") or None
    if modality == "llm":
        from flows.finetune import finetune_flow
        return finetune_flow(
            base_model=base or os.getenv("BASE_MODEL", "Qwen/Qwen2.5-0.5B-Instruct"),
            steps=int(req.get("steps", 10)), lora_r=int(req.get("lora_r", 8)), **common)
    if modality == "vision":
        from flows.vision_finetune import vision_finetune_flow
        return vision_finetune_flow(
            base_model=base, epochs=int(req.get("epochs", 3)), lr=float(req.get("lr", 1e-3)),
            batch_size=int(req.get("batch_size", 8)),
            unfreeze_epochs=int(req.get("unfreeze_epochs", 0)), **common)
    if modality == "embeddings":
        from flows.embeddings_finetune import embeddings_finetune_flow
        return embeddings_finetune_flow(
            base_model=base, epochs=int(req.get("epochs", 1)),
            batch_size=int(req.get("batch_size", 16)),
            warmup_ratio=float(req.get("warmup_ratio", 0.1)), **common)
    if modality == "asr":
        from flows.asr_finetune import asr_finetune_flow
        return asr_finetune_flow(
            base_model=base, epochs=int(req.get("epochs", 3)), lr=float(req.get("lr", 1e-4)),
            grad_accum=int(req.get("grad_accum", 4)), warmup_ratio=float(req.get("warmup_ratio", 0.1)),
            lora_r=int(req.get("lora_r", 8)), quant=req.get("quant", "q8_0"), **common)
    raise ValueError(f"unknown modality {modality!r} (expected {'|'.join(VALID_MODALITIES)})")
