# User Guide — LAN Self-Service GPU Broker

> **Status: PLANNED UX (spec 026, pre-implementation).** This guide describes how the broker is
> *intended* to work per [spec.md](./spec.md). Exact endpoints, command names, and flags are
> **illustrative** and will be finalized in `/speckit-plan` (contracts) and `/speckit-implement`.
> Each section notes the spec story/FR it traces to.

## What this is

The GPU broker lets people, devices, and services **on your local network** share the single GPU on
the host machine — for **inference**, **training/batch jobs**, and **interactive notebook sessions** —
without the owner mediating each request. You authenticate with an API key; your usage is metered and
capped by a quota.

There are two audiences:
- **Tenants** — anyone on the LAN who consumes the GPU (a teammate, your own laptop/phone, an app, a script).
- **Owner** — the person who runs the host, issues keys, sets quotas, and watches the queue.

### Key concepts

| Term | Meaning |
|---|---|
| **Tenant** | An identity that consumes the GPU, identified by an API key. |
| **API key** | Your bearer credential. Issued and revoked by the owner. Don't share it. |
| **GPU-seconds** | The canonical unit of usage. Shown in the UI as **"credits"** (same thing). |
| **Quota** | Your budget of GPU-seconds for a **recurring window** (e.g., daily/monthly) that auto-resets. |
| **Shapes** | The three ways to use the GPU: **inference**, **jobs**, **interactive sessions**. |
| **Lease** | The GPU admission grant. Inference tenants can share VRAM (co-resident); jobs get the whole GPU. |

---

## 1. Getting access *(traces to US1 · FR-002/003/004)*

1. Ask the **owner** to create a tenant and issue you an **API key**.
2. You'll receive: the **broker address** (a LAN URL over **HTTPS**, e.g. `https://gpu.lan:8443`) and your
   **key**. The owner also gives you the broker's **CA certificate** to trust — a home-LAN broker normally
   uses a private CA, so your client needs it (`curl --cacert broker-ca.pem`, or install it in your OS/
   Python trust store) rather than disabling verification.
3. Put the key in an `Authorization: Bearer <key>` header on every request. Requests without a valid key
   are refused and never touch the GPU.

> **Always HTTPS.** Your key is a reusable credential sent on every request — over plaintext `http://`
> anyone on the LAN can capture it and use your quota as you, indefinitely. Never pass `-k` /
> `verify=False` to "make it work": that silently discards the protection. Plain `http://` is acceptable
> only when the owner runs single-tenant on `localhost`, where nothing leaves the machine.

> The broker is **LAN-only** — it is not reachable from the public internet. You must be on the local
> network (the owner's remote-access setup is separate and not for tenants).

---

## 2. Inference — the "just a URL" path *(traces to US1 & US4 · FR-005/006/018)*

Inference speaks an **OpenAI-compatible API**, so existing tools/SDKs work by just changing the base URL.

**Chat (LLM):**
```bash
curl --cacert broker-ca.pem https://gpu.lan:8443/v1/chat/completions \
  -H "Authorization: Bearer $BROKER_KEY" \
  -d '{"model":"qwen","messages":[{"role":"user","content":"Hello"}]}'
```

**From code (Python, OpenAI SDK):**
```python
import httpx
from openai import OpenAI
# Trust the broker's private CA — do NOT set verify=False, which discards the protection entirely.
client = OpenAI(base_url="https://gpu.lan:8443/v1", api_key=BROKER_KEY,
                http_client=httpx.Client(verify="broker-ca.pem"))
client.chat.completions.create(model="qwen", messages=[{"role":"user","content":"Hi"}])
client.embeddings.create(model="bge", input=["text to embed"])                 # embeddings
client.audio.transcriptions.create(model="whisper", file=open("clip.wav","rb")) # speech-to-text
```

**Computer vision** (classification/detection) uses a task-typed endpoint (tensor in / result out) — the
exact shape is defined in the plan's contracts.

**Point an existing app at it:** set your app's `OPENAI_BASE_URL` to the broker and its key to your tenant
key — it now runs private, on your hardware.

Concurrent requests from several tenants against a resident model are **interleaved** — everyone shares.

---

## 3. Jobs — training & batch *(traces to US2 · FR-008–013, FR-025/026)*

When you have a *workload* (a fine-tune or a batch run), submit it as a **job** instead of holding the GPU
yourself. The broker queues it, runs it with **exclusive** GPU access when free, streams logs, and produces
artifacts (a fine-tune also registers a model with lineage).

```bash
broker submit --image myimg:latest --gpu 1 \
  --data ./dataset --out ./results -- python train.py --epochs 3
# → Job j-1a2b queued (lane: jobs, position 2)

broker queue            # see the queue and your position
broker logs j-1a2b -f   # stream logs once it's running
broker status j-1a2b    # final status + artifact/model location
```

**Fine-tune** shortcut (uses the platform's multimodal trainer):
```bash
broker finetune --base qwen-0.5b --data ./set --modality vision --epochs 3
```

Important on one GPU:
- A running job is **never interrupted** — inference and other jobs wait until it finishes.
- Every job runs inside a **hardened sandbox** (non-root, no host filesystem access, restricted network) —
  so it's safe to run others' jobs, and your job can't reach the host or other tenants.

---

## 4. Interactive notebook sessions *(traces to US5 · FR-020/021/022)*

For exploratory work you can open a **GPU-backed notebook session**. Because a session would otherwise hold
the whole GPU, it has guard rails:

```bash
broker session start --gpu 1 --ttl 2h   # → returns a notebook URL
```
- **Idle-cull**: if the session does no GPU work for the configured idle window, the GPU is released.
- **TTL**: the session ends at its maximum lifetime.
- **Train from a notebook the right way**: don't run a long training loop in the kernel (it hogs the GPU the
  whole time). Instead submit it as a job from a cell (`broker.finetune(...)`) and stream logs back — the GPU
  is held only for the run.

---

## 5. Quotas & usage *(traces to US3 · FR-014/015/016/017)*

- Check your balance:
  ```bash
  broker usage        # GPU-seconds used / remaining this window, recent activity
  ```
- Usage is metered in **GPU-seconds** ("credits" in the UI = the same number).
- When your **window budget** is exhausted, further GPU work is refused until the window **auto-resets** — no
  need to ask the owner to top you up under normal use.
- Everything you run is recorded to an append-only ledger attributed to you.

---

## 6. What to expect on a single GPU (please read)

The host has **one** GPU, so behavior is honest about contention:

- **Inference scales to many users** — small serving models can be **co-resident** and shared; concurrent
  callers interleave. This is the smooth, many-tenants path.
- **Jobs are one-at-a-time and exclusive** — while a job runs, it owns the GPU; other jobs and inference
  **queue** behind it (jobs are never preempted). A long training run *will* block others until it's done.
- **Interactive sessions are the greediest** — use them sparingly; they're idle-culled and TTL'd on purpose.
- **Ordering**: inference is favored ahead of exclusive jobs (shape lanes), and jobs run first-come-first-served.

If you get a "GPU busy" response, a job is holding the GPU — retry shortly or check `broker queue`.

---

## 7. Owner / admin guide *(traces to FR-004/014/017, FR-001/026)*

**Issue & revoke access**
```bash
broker admin tenant create --name alice          # → prints a new API key (share securely)
broker admin tenant revoke --name alice          # kills the key; in-flight work ends gracefully
```

**Set quotas** (per tenant, per recurring window)
```bash
broker admin quota set --tenant alice --window daily --gpu-seconds 3600
```

**Watch the system** — the operator console (021 loop-native console) shows:
- current **queue depth** and lane ordering,
- the **resident** serving tenants (and VRAM budget headroom),
- **per-tenant usage** against quota.

**Networking (LAN-only)**
- The broker binds to the LAN interface. Because serving runs inside WSL, LAN reachability is provided by a
  `netsh portproxy` bridge on the Windows host **or** WSL mirrored-networking mode (chosen in the plan).
- **Do not** port-forward the broker on your router — it must stay off the public internet.
- Give tenants a **DHCP-reserved** host IP (or an mDNS name like `gpu.lan`) so the address is stable.

**Isolation** — all jobs run in a hardened sandbox by default; there is no "trusted tenant" bypass.

---

## 8. Errors & troubleshooting

| You see | Meaning | Do |
|---|---|---|
| `401 / unauthorized` | Missing/invalid API key | Check the `Authorization: Bearer` header; ask owner to re-issue |
| `403 / quota exhausted` | Your window budget is used up | Wait for the window reset, or ask the owner to raise the quota |
| `503 / gpu_busy` | Something else has the GPU right now — an exclusive job, another tenant's model loading, or not enough free VRAM this instant | **Retry** — this one always clears. Honour the `Retry-After` header; check `broker queue` to see what's ahead of you |
| `413 / model_too_large` | The model wouldn't fit even on a completely empty GPU | Retrying will not help. Choose a smaller model |
| Can't reach the broker at all | You're off-LAN, or address changed | Get on the local network; confirm the host IP / `gpu.lan` name |

---

## Traceability

This guide maps to the spec: **US1** inference (§2), **US2** jobs (§3), **US3** quotas/ledger/visibility
(§5, §7), **US4** modalities (§2), **US5** interactive sessions (§4); single-GPU behavior (§6) reflects the
amended **Principle II** (constitution v1.6.0) and FR-025 scheduling. Command/endpoint specifics are finalized
in `plan.md` contracts.
