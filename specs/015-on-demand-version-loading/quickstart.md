# Quickstart / Validation Guide: Score-at-Registration (015)

On-hardware validation that every version is born with its eval metric, the HPO objective is meaningful,
and one-model-in-VRAM holds. Run on the reference machine (Win11 + WSL2 + RTX 5070 Ti).

## Prerequisites

```powershell
./scripts/up_all.ps1            # stack up, 6 daemons healthy
$KEY = (Get-Content .env | Select-String '^GATEWAY_API_KEYS=').ToString().Split('=')[1].Split(',')[0]
```

WSL trainer venv `~/mlops-train`, plus the already-built `~/llama.cpp` and `~/whisper.cpp` binaries.

## Scenario 1 — Every fine-tune is born with a metric (SC-087, SC-092)

For each modality (vision is fastest; LLM/ASR exercise the transient-scorer path):

```bash
# launch a fine-tune (Runs API) → on completion, the new version must carry an eval_<metric> tag
curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' -X POST localhost:8080/runs \
  -d '{"modality":"vision","dataset_name":"vision-demo","dataset_version":"<ver>","output_name":"v015-vision"}'
# then inspect the registered version's tags — expect metric/value/direction/benchmark/benchmark_hash
curl -s -H "X-API-Key: $KEY" localhost:8080/models/v015-vision | jq '.versions[0].tags'
```

**Expected**: the version has `eval_<metric>` logged at registration (no separate evaluate call). Repeat
for `embeddings` (recall@k) and `asr` (WER) — both must score against the **new** benchmark fixtures.

## Scenario 2 — One model in VRAM through train→score (SC-088)

While a LLM/ASR fine-tune runs, sample VRAM:

```bash
watch -n2 'nvidia-smi --query-gpu=memory.used --format=csv,noheader'
```

**Expected**: usage rises for training, drops as the training model is freed, rises again for the transient
GGUF/ggml scorer, drops at release — **never two models resident at once**.

## Scenario 3 — Meaningful HPO objective, no hostname error (SC-089)

```bash
curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' -X POST localhost:8080/studies \
  -d '{"dataset_name":"vision-demo","dataset_version":"<ver>","output_name":"v015-hpo","modality":"vision","n_trials":2}'
# poll GET /studies/{id}
```

**Expected**: both trials log **distinct** objective values (each its own trained version's metric), a best
trial is registered, and **no** `[Errno -2] Name or service not known` appears (finding #4 closed).

## Scenario 4 — Compare reads logged metrics; gateway guard (SC-090, SC-091)

```bash
# two scored versions → compare reads logged metrics, no reload
curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' -X POST localhost:8080/models/<name>/compare \
  -d '{"challenger":"<v2>"}'
# a non-@serving version with no logged metric → clear error, not a score
curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' -X POST localhost:8080/models/<name>/evaluate \
  -d '{"version":"<unscored-non-serving>"}'
```

**Expected**: `compare` returns a real verdict from logged metrics; the unscored `/evaluate` returns the
clear guard error (SC-091), never a silent resident-model score.

## Scenario 5 — No regression (SC-093)

```bash
GATEWAY_API_KEY=$KEY ~/mlops-train/bin/python -m pytest -q   # 001–014 still green (GPU-tenant tests in isolation)
```

See [contracts/evaluate-guard.md](contracts/evaluate-guard.md) and [data-model.md](data-model.md) for the
guard behavior and the per-modality scorer contract.
