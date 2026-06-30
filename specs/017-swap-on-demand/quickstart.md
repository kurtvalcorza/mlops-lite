# Quickstart / Validation Guide: Swap-on-Demand (017)

On-hardware validation that an operator can swap a resident serving model, training is never preempted,
in-flight drains, and the default path is unchanged. Run on the reference machine (RTX 5070 Ti).

```powershell
./scripts/up_all.ps1
$KEY = (Get-Content .env | Select-String '^GATEWAY_API_KEYS=').ToString().Split('=')[1].Split(',')[0]
```

## Scenario 1 — Swap a resident serving model (SC-100)

```bash
# make the LLM resident (one /infer), then classify WITH preempt
curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' -X POST localhost:8080/infer -d '{"prompt":"hi"}'
curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -X POST localhost:8080/vision/classify -d '{"image_b64":"<...>","preempt":true}'
# watch VRAM during the swap:
#   nvidia-smi --query-gpu=memory.used --format=csv,noheader
```

**Expected**: the LLM is evicted (drain→unload→release), the vision model loads + returns labels; VRAM shows
the LLM free **before** vision loads — **never two models resident**.

## Scenario 2 — Default (no preempt) is unchanged (SC-101)

```bash
# LLM resident; classify WITHOUT preempt
curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -X POST localhost:8080/vision/classify -d '{"image_b64":"<...>"}'
```

**Expected**: `409 GPU busy` — identical to 008 refuse-if-held. No swap.

## Scenario 3 — Training is never preempted (SC-102)

```bash
# start a fine-tune (holds the lease), then try to swap with preempt
curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' -X POST localhost:8080/runs -d '{...}'
curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -X POST localhost:8080/vision/classify -d '{"image_b64":"<...>","preempt":true}'
```

**Expected**: `409 { "detail": "training in progress — not preemptable" }`; the fine-tune **completes
unaffected** (never evicted).

## Scenario 4 — In-flight drains (SC-103)

Issue a long `/infer` on the resident LLM, then a `preempt=true` classify mid-generation.

**Expected**: the in-flight generation **completes** (or is cut only after the drain timeout); the swap then
proceeds. No silent drop in the common case.

## Scenario 5 — UI Swap & classify (SC-104)

In the Infer tab with the LLM resident: the classify control shows **"Swap & classify (~2.5s, evicts LLM)"**;
confirming performs the swap end-to-end.

## Scenario 6 — No regression (SC-105)

```bash
GATEWAY_API_KEY=$KEY ~/mlops-train/bin/python -m pytest -q   # 001–016 green (GPU tests in isolation)
```

See [contracts/unload-now-endpoint.md](contracts/unload-now-endpoint.md),
[contracts/preempt-flag.md](contracts/preempt-flag.md), and [data-model.md](data-model.md).
