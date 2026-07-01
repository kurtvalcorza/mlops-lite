# Quickstart / Validation Guide: Shadow-Replay (016)

On-hardware validation that capture is bounded, a challenger can be replayed over production traffic with
an advisory verdict, one model stays in VRAM, and the gate is untouched. Run on the reference machine.

> Prereq: **015 merged** (016 reuses its in-process scorers). Stack up via `up_all.ps1`; capture on
> (`QUALITY_CAPTURE_IO=1`) with a sampling/cap/TTL policy set in `.env`.

```powershell
$KEY = (Get-Content .env | Select-String '^GATEWAY_API_KEYS=').ToString().Split('=')[1].Split(',')[0]
```

## Scenario 1 — Bounded, recoverable capture (SC-094)

```bash
# serve a handful of requests per modality with capture on
curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' -X POST localhost:8080/infer -d '{"prompt":"..."}'
# inspect the results bucket: recoverable inputs present (prompt/image/audio), bounded by cap/TTL
# (mc / boto: list the inputs/ prefix; confirm count <= SHADOW_CAPTURE_CAP_N)
```

**Expected**: a bounded sample of **recoverable** inputs stored (LLM prompt, vision image, ASR audio);
older ones pruned by cap/TTL; serving latency unchanged. With `QUALITY_CAPTURE_IO=0` → nothing stored.

## Scenario 2 — Shadow-replay a challenger → advisory verdict (SC-095, SC-097)

```bash
# attach labels for a window of served predictions (013), then:
curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -X POST localhost:8080/models/<name>/shadow-replay -d '{"challenger":"<v>","window_n":100}'
# → {shadow_id, status:queued}; poll the GET
curl -s -H "X-API-Key: $KEY" localhost:8080/models/<name>/shadow-replay/<shadow_id>
```

**Expected**: a verdict comparing **challenger (replayed)** vs **champion (logged)** on the same
`(input,label)` window, honouring the modality metric/direction, `advisory:true`. The promotion gate
(011/015) is unchanged — promoting still uses the held-out verdict.

## Scenario 3 — One model in VRAM (SC-096)

```bash
watch -n2 'nvidia-smi --query-gpu=memory.used --format=csv,noheader'   # during the replay job
```

**Expected**: the challenger loads, scores, frees — **never** two models resident (lease-serialized); the
champion is **not** re-run (its value comes from logs).

## Scenario 4 — Insufficient / no corpus (SC-098)

```bash
# with fewer than MIN_PAIRS captured∩labeled → insufficient_data
curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -X POST localhost:8080/models/<name>/shadow-replay -d '{"challenger":"<v>"}'
# with QUALITY_CAPTURE_IO=0 → no_corpus
```

**Expected**: explicit `insufficient_data` / `no_corpus` — never a misleading verdict from a few pairs.

## Scenario 5 — No regression (SC-099)

```bash
GATEWAY_API_KEY=$KEY ~/mlops-train/bin/python -m pytest -q   # 001–015 still green (GPU tests in isolation)
```

See [contracts/shadow-replay-endpoint.md](contracts/shadow-replay-endpoint.md),
[contracts/capture-extension.md](contracts/capture-extension.md), and [data-model.md](data-model.md).
