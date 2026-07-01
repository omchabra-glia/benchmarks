# No-Docker SWE-bench on the OpenHands Remote Runtime

Run **real SWE-bench_Verified** instances end-to-end — agent + grading — with **no local
Docker**, **no custom image builds**, and **no CI**. The only infrastructure is the
OpenHands remote runtime (a `RUNTIME_API_KEY`) plus an LLM endpoint.

This is the fast iteration loop for building a better agentic system: change the agent
(`build_agent()` in `harness.py`), rerun on a few instances, read the resolved rate.

---

## TL;DR

```bash
# from repo root, after `make build`
export RUNTIME_API_KEY="ah-..."                       # runtime.eval.all-hands.dev
export LLM_MODEL="azure_ai/DeepSeek-V4-Pro"           # any LiteLLM model id
export LLM_BASE_URL="https://<foundry>.services.ai.azure.com/models"  # note the /models
export LLM_API_VERSION="2024-05-01-preview"           # Azure Foundry only
export LLM_API_KEY="..."

uv run python swebench_nodocker/harness.py \
  --instances django__django-11099 astropy__astropy-14995 \
  --workers 8 --max-iter 80
# -> RESOLVED n/m  and swebench_nodocker/results.jsonl
```

~**2 minutes per instance** wall-clock (31s bootstrap + ~60s agent + grading), fully
parallel across `--workers`.

---

## Why this exists (the problem it solves)

Two "normal" ways to run OpenHands SWE-bench both hit walls here:

| Path | Wall |
|------|------|
| `--workspace docker` | needs a local Docker daemon (we have none; Mac/arm makes x86 images painful) |
| `--workspace remote` with pre-built `eval-agent-server` images | those public images are ~6 months stale (Jan 2026 SDK) and **no longer boot** on the current runtime; building fresh ones needs a beefy Docker host or paid GitHub large runners |

**Insight that unblocks it:** the remote runtime can boot *any* public image and run an
*arbitrary* start command. The **official swebench base images**
(`docker.io/swebench/sweb.eval.x86_64.<inst>`) are current and maintained, and the
OpenHands agent-server is on **PyPI**. So we boot the official image and *bootstrap the
agent-server from PyPI at container start*. No building anything.

---

## How it works (architecture)

```
harness.py (your machine, thin client)
   │  POST /start  { image: docker.io/swebench/...<inst>,
   │                 command: ["bash","-lc", <bootstrap>] }   ← command MUST be an array
   ▼
remote runtime pod (runtime.eval.all-hands.dev, GKE)
   ├─ bootstrap: uv venv py3.12 → pip install openhands-agent-server+tools (--no-cache)
   ├─ exec agent-server on :60000   → pod goes "ready" (serves /ready,/server_info)
   ├─ OpenHands agent loop runs here; edits repo at /testbed; calls your LLM directly
   └─ grading: swebench eval script runs the FAIL_TO_PASS / PASS_TO_PASS tests here
   ▲
   │  events stream back; final git diff pulled; resolved verdict computed locally
```

Grading is done **in the same sandbox** using the official `swebench` package
(`make_test_spec` → run `eval_script` → `get_eval_report`). No Modal, no Docker.

---

## Hard-won gotchas (all handled in `harness.py`)

1. **`command` must be a JSON array.** A string command is naively split on whitespace,
   so a shell one-liner with quotes/pipes breaks. Use `["bash","-lc", script]`.
2. **`uv` cache crashes the pod.** Installing without `--no-cache` reliably kills the
   container mid-install (not disk — 115G free; not memory — `resource_factor=8` same).
   `uv pip install --no-cache` + `UV_NO_CACHE=1` fixes it. Install completes in ~30s.
3. **Readiness needs an HTTP endpoint, not just a bound port.** The runtime probes
   `/ready`/`/server_info`. The debug fallback server returns 200 on *all* paths so a
   failed bootstrap still goes "ready" and its `/boot.log` is fetchable.
4. **`openhands-agent-server` does NOT pull `openhands-tools`.** Install both, or the
   agent's `terminal`/`file_editor` tool calls fail server-side.
5. **Pin `AS_VER` to your local SDK version** (default `1.27.0`) so the client and the
   in-sandbox agent-server speak the same protocol.
6. **Azure Foundry LLM**: `base_url` must end in `/models`, and set
   `api_version=2024-05-01-preview` (the SDK's auto-default `2024-12-01-preview` 404s).

---

## Iterating on the agent (what's fast vs slow)

Everything you need to build a better agent is **fast** — it ships to the sandbox per run,
no image rebuild:

- **Tier 1 — orchestration / MAS** (client-side Python in `harness.py`): best-of-N,
  a planner→worker split, critic/verify passes, cross-run patch voting. Edit `run_one`.
- **Tier 2 — agent config** (`build_agent()`): system prompt (as an inline string),
  which tools are enabled, condenser, critic, subagent definitions.
- **Tier 3 — `client_tools`**: custom tool code that runs on your machine via callback.

**Slow (avoid):** changing the agent-server / tool *internals* that run in the sandbox —
that would need a new image or a `plugins` git-ref. Not needed for prompt/orchestration work.

Inner loop: iterate agent *logic* on 1–5 instances; only run larger sets for milestones.

---

## Files

- `harness.py` — the runner. `build_agent()` is the edit point for agent changes.
- `results.jsonl` — per-instance `{instance_id, resolved, patch, report, error}`.

## Notes / limits

- Instance IDs must exist in the chosen dataset/split (`SWE-bench_Verified` test here).
- `--workers N` runs N sandboxes concurrently; each is independent. The runtime key's
  quota bounds N.
- Bootstrap installs from PyPI each boot (~30s). Acceptable for iteration; if you later
  want to eliminate it, bake a custom image with a Docker host and point
  `OPENHANDS_EVAL_AGENT_SERVER_IMAGE` at it.
