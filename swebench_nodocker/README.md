# swebench-nodocker

Run **real SWE-bench** (OpenHands agent + grading) on the OpenHands remote runtime with
**no local Docker, no Docker-in-Docker, no custom image builds, and no CI**. Deploy the
runner itself in a plain (unprivileged) container or k8s pod — it's a thin HTTP client.

Built for fast iteration on the *agentic system*: change the agent in one function,
rerun on a few instances, read the resolved rate.

---

## Quickstart

```bash
# from the benchmarks repo root, once:
make build

# set credentials
export RUNTIME_API_KEY="ah-..."                      # runtime.eval.all-hands.dev
export LLM_MODEL="azure_ai/DeepSeek-V4-Pro"          # any LiteLLM model id
export LLM_BASE_URL="https://<foundry>.services.ai.azure.com/models"   # note /models
export LLM_API_VERSION="2024-05-01-preview"          # Azure Foundry only
export LLM_API_KEY="..."

# run a few instances
uv run python swebench_nodocker/harness.py \
  --instances django__django-11099 astropy__astropy-12907 \
  --workers 2 --max-iter 80
# -> RESOLVED n/m  + swebench_nodocker/results.jsonl
```

~**2 min/instance** (≈30s bootstrap + agent + grading), parallel across `--workers`.

## How it works (one line each)

1. Boot the **official** SWE-bench image (`docker.io/swebench/sweb.eval.x86_64.<inst>`) on the runtime — *the runtime pulls/runs it; you never touch Docker*.
2. **Bootstrap** `openhands-agent-server` from **PyPI** at container start (no custom image).
3. Run the OpenHands agent against the repo at `/testbed`; it calls your LLM directly.
4. **Grade in the same sandbox** with the official `swebench` eval script + parser.

Full architecture, design rationale, and the hard-won gotchas: see **[SETUP.md](./SETUP.md)**.

## Iterating on the agent

- **`build_agent()`** — system prompt, tools, condenser, critic. (tier-2 config)
- **`run_one()`** — orchestration / multi-agent: best-of-N, planner→worker, patch voting. (tier-1)

Both ship to the sandbox **per run** — no rebuilds, no redeploys. Edit → rerun → read resolved rate.

## Running it in k8s (to share with a team)

A **normal, unprivileged pod** — no docker socket, no `privileged`, no DinD. Requirements:

- Python 3.12 + repo deps (`uv sync`)
- Network egress to: the runtime API, your LLM endpoint, PyPI + `astral.sh`, Hugging Face
- Secrets (env / k8s Secret): `RUNTIME_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`, `LLM_API_VERSION`, `LLM_API_KEY`

Docker Hub pulls and container execution happen entirely on the runtime side.

## Environment variables

| Var | Required | Notes |
|-----|----------|-------|
| `RUNTIME_API_KEY` | ✅ | runtime.eval.all-hands.dev key |
| `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY` | ✅ | LiteLLM-style model config |
| `LLM_API_VERSION` | Azure only | e.g. `2024-05-01-preview` |
| `RUNTIME_API_URL` | ⬜ | default `https://runtime.eval.all-hands.dev` |
| `AS_VER` | ⬜ | agent-server/tools version to install (default `1.27.0`; match your SDK) |
| `LLM_NUM_RETRIES` / `LLM_RETRY_MAX_WAIT` | ⬜ | rate-limit resilience (default 10 / 120s) |

## Known limits

- **Throughput is bounded by your LLM rate quota**, not the runtime. High `--workers` on a
  shared endpoint throttles; iterate at `--workers 2–4`, raise the quota for big scored runs.
- Bootstrap installs from PyPI each boot (~30s). To eliminate it at scale, bake a custom
  `swebench-base + agent-server` image with a Docker host and point the harness at it.

## Files

- `harness.py` — the runner (edit `build_agent()` / `run_one()`).
- `SETUP.md` — architecture + gotchas.
- `results.jsonl` — per-instance `{instance_id, resolved, patch, report, error}`.
