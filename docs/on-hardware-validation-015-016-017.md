# On-hardware validation runbook — increments 015 · 016 · 017

The cloud/CI environment has **no NVIDIA GPU**, so every success criterion that loads a model, samples
`nvidia-smi`, or drives a live daemon is validated **here, on the reference box** (Win11 + WSL2 + RTX
5070 Ti). Everything else (the offline unit suites) is already green in CI.

This runbook consolidates the three per-increment quickstarts
([015](../specs/015-on-demand-version-loading/quickstart.md),
[017](../specs/017-swap-on-demand/quickstart.md), and 016's `specs/016-shadow-replay/quickstart.md`) into
one pass. Run it **after 015/016/017 are all deployed** (015 is merged; check out/merge 016 + 017 first).
Tick each SC as you go.

## 0. Bring-up

```powershell
./scripts/up_all.ps1                      # infra + 6 native daemons healthy
# ASR is opt-in — build + enable whisper.cpp if you're validating the ASR SCs:
#   bash serving/whispercpp/build.sh ; then add asr to SUPERVISE_DAEMONS and restart the supervisor
$KEY = (Get-Content .env | Select-String '^GATEWAY_API_KEYS=').ToString().Split('=')[1].Split(',')[0]
```

WSL trainer venv `~/mlops-train`, plus the already-built `~/llama.cpp` and `~/whisper.cpp` binaries. Keep a
VRAM monitor open in a second pane for the one-model-in-VRAM checks:

```bash
watch -n1 'nvidia-smi --query-gpu=memory.used --format=csv,noheader'
```

---

## 015 — Score-at-registration (closes SC-068)

### ☐ SC-087 / SC-092 — every fine-tune is born with its metric (all 4 modalities)

For each modality, launch a fine-tune and confirm the registered version carries an `eval_*` tag logged
**at registration** (no separate `/evaluate` call). Vision is fastest; LLM + ASR exercise the transient
llama.cpp / whisper.cpp scorers; embeddings exercises the in-memory recall@k scorer.

```bash
for M in vision embeddings asr llm; do
  curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' -X POST localhost:8080/runs \
    -d "{\"modality\":\"$M\",\"dataset_name\":\"<$M-demo>\",\"dataset_version\":\"<ver>\",\"output_name\":\"v015-$M\"}"
done
# after each completes, inspect the newest version's tags:
curl -s -H "X-API-Key: $KEY" localhost:8080/models/v015-vision | jq '.versions[0].tags'
```

**PASS** when each new version has `eval_metric` / `eval_value` / `eval_direction` / `eval_benchmark` /
`eval_benchmark_hash`, scored on the shipped fixture (embeddings→`recall_at_k`, asr→`wer`, vision→
`accuracy`, llm→`task_accuracy`). Fine-tune two *behaviorally-distinct* versions of one model → the two
logged metrics **differ** (each scored its own model, not a shared resident one).

### ☐ SC-088 — one model in VRAM through train→score

While an LLM or ASR fine-tune runs, watch the VRAM pane: usage rises for training, **drops** as the
training model is freed, rises again for the transient GGUF/ggml scorer, drops at release. **PASS** =
never two models resident at once.

### ☐ SC-089 / SC-090 — meaningful HPO objective + compare, no hostname error

```bash
curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' -X POST localhost:8080/studies \
  -d '{"dataset_name":"vision-demo","dataset_version":"<ver>","output_name":"v015-hpo","modality":"vision","n_trials":2}'
# poll GET /studies/{id}
```

**PASS** = both trials log **distinct** objective values (each its own trained version's logged metric), a
best is registered, and **no** `[Errno -2] Name or service not known` / `host.docker.internal` appears in
the trainer logs (finding #4 closed). Then `compare` two scored versions → a real verdict from logged
metrics, no model reload.

### ☐ SC-091 — gateway `/evaluate` guard

```bash
# a non-@serving version with no logged metric → clear 409, NOT a silent resident-model score:
curl -s -o /dev/null -w '%{http_code}\n' -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -X POST localhost:8080/models/<name>/evaluate -d '{"version":"<unscored-non-serving>"}'
```

**PASS** = `409` with the guard message ("… not the @serving model and has no logged eval metric …"); the
`@serving` version still scores (`200`); a registration-scored version returns its logged metric.

### ☐ SC-093 — no regression (001–014)

```bash
GATEWAY_API_KEY=$KEY ~/mlops-train/bin/python -m pytest -q     # GPU-tenant tests validated in isolation
```

---

## 016 — Shadow-replay champion-challenger (advisory)

Enable input capture first (it's behind the opt-in): `QUALITY_CAPTURE_IO=1` and set
`SHADOW_CAPTURE_SAMPLE=1.0` for the test (defaults are fine). Serve a batch of labeled requests per
modality (LLM/vision/ASR) and attach labels via `POST /monitor/label {prediction_id, label}` so a
`captured ∩ labeled` window accumulates.

### ☐ SC-094 — bounded, recoverable capture; off ⇒ none; serving unaffected

```bash
# with capture ON, serve a few per modality, then confirm recoverable inputs landed:
#   inputs/<modality>/... exists in the MinIO `results` bucket (mc ls / console)
# set SHADOW_CAPTURE_CAP_N small (e.g. 5) → older captures pruned; set QUALITY_CAPTURE_IO=0 → none stored.
```

**PASS** = bounded recoverable inputs stored per modality (cap/TTL enforced); capture off → nothing
recoverable; served-request latency unchanged (fire-and-forget).

### ☐ SC-095 / SC-096 / SC-097 — replay a challenger → advisory verdict, one model in VRAM, gate unchanged

```bash
curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -X POST localhost:8080/models/<name>/shadow-replay -d '{"challenger":"<v>"}'   # → 202 {shadow_id,...}
curl -s -H "X-API-Key: $KEY" localhost:8080/models/<name>/shadow-replay/<shadow_id> | jq
```

**PASS** = the verdict compares the challenger (replayed) vs the champion (from **logged** predictions, no
re-run) on the same `(input,label)` window, `"advisory": true`; the VRAM pane shows **one** model resident
during the replay (challenger loaded under the lease); a `promote` still runs the **unchanged** 011/015
gate (shadow verdict never blocks it).

### ☐ SC-098 — honest degradation

```bash
# too few captured∩labeled pairs → insufficient_data; QUALITY_CAPTURE_IO=0 → no_corpus:
curl -s -o /dev/null -w '%{http_code}\n' -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -X POST localhost:8080/models/<name>/shadow-replay -d '{"challenger":"<v>"}'
```

**PASS** = `409` with `status: insufficient_data` (with `n_pairs`/`min`) or `no_corpus` — never a verdict
from thin data.

> **Note:** the trainer-side challenger artifact loader (`build_challenger_predict_fn`, per-modality fetch
> of the GGUF/ggml/model.pt by version + the 015 scorer) is the one on-hardware wiring step; it raises a
> clear `NotImplementedError` until wired. Wire it before running SC-095/096.

---

## 017 — Swap-on-demand (operator-confirmed preemptive serving)

### ☐ SC-101 — default (no `preempt`) is byte-for-byte 008

With a serving model resident, issue a *different* serving request **without** `preempt` → the usual
`409 GPU busy`. **PASS** = identical to 008 refuse-if-held (no swap, no surprise).

### ☐ SC-100 / SC-104 — operator-confirmed swap works; one model in VRAM

```bash
# LLM resident (do an /infer first), then classify with preempt=true:
curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' -X POST localhost:8080/vision/classify \
  -d '{"image_b64":"<...>","preempt":true}'
```

**PASS** = the LLM is evicted (drain → unload → release) and the vision model loads + returns; the VRAM
pane shows the LLM free **before** vision loads — **never two resident**. In the UI Infer tab, the
"GPU busy" classify state offers **"Swap & classify"** (cost-stated) and works on confirm (SC-104).

### ☐ SC-102 — training is never preempted

```bash
# start a fine-tune (holds the lease), then a preempt=true serving request:
curl -s -o /dev/null -w '%{http_code}\n' -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -X POST localhost:8080/vision/classify -d '{"image_b64":"<...>","preempt":true}'
```

**PASS** = `409 "training in progress — not preemptable"`; the fine-tune **completes unaffected** (never
evicted).

### ☐ SC-103 — in-flight requests drain

Issue a long inference on the holder, then a `preempt=true` swap. **PASS** = the in-flight request
completes (or is cut only after `SWAP_DRAIN_TIMEOUT_S`) before the swap proceeds — no silent drop in the
common case.

### ☐ UI build (no node_modules in CI)

```bash
cd ui && npm ci && npx tsc --noEmit && npm run build      # ClassifyPanel.tsx "Swap & classify"
```

### ☐ SC-105 — no regression (001–016) + default==008

```bash
GATEWAY_API_KEY=$KEY ~/mlops-train/bin/python -m pytest -q     # GPU-tenant tests in isolation
```

---

## Sign-off

| Increment | SCs | Result |
|---|---|---|
| 015 | SC-087 · 088 · 089 · 090 · 091 · 092 · 093 | ☐ |
| 016 | SC-094 · 095 · 096 · 097 · 098 | ☐ |
| 017 | SC-100 · 101 · 102 · 103 · 104 · 105 | ☐ |

When all pass, the three increments are hardware-validated end-to-end. The offline suites, secret-scan, and
`claude-review` are already green in CI (see PRs #22/#24/#23).
