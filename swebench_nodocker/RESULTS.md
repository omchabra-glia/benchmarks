# Baseline Results — No-Docker SWE-bench on the OpenHands Remote Runtime

Stock OpenHands default agent, DeepSeek-V4-Pro (via Glia Azure Foundry), run through
`swebench_nodocker/harness.py`. All numbers **measured** (token usage from
`conversation_stats.get_combined_metrics()`), not estimated.

## Headline

| Metric | Value |
|---|---|
| Resolved | **5 / 8 (62.5%)** |
| Wall-clock (8 in parallel) | **~12.5 min** (11:49:00 → 12:01:38) |
| Total cost | **$4.46** |
| Tokens | 6.21M prompt · 70.7K completion |
| Concurrency | `--workers 8`, `--max-iter 80` |
| LLM rate-limit failures | **0** (after raising Foundry quota to 1M TPM) |

## Per-instance

| Instance | Result | Cost | Prompt tok | Completion tok |
|---|---|---|---|---|
| astropy__astropy-12907 | ✅ resolved | $0.52 | 768,750 | 7,849 |
| matplotlib__matplotlib-13989 | ✅ resolved | $0.52 | 682,638 | 7,214 |
| pytest-dev__pytest-10051 | ✅ resolved | $0.60 | 664,701 | 6,047 |
| scikit-learn__scikit-learn-10297 | ✅ resolved | $0.43 | 519,751 | 3,791 |
| sympy__sympy-11618 | ✅ resolved | $0.68 | 664,315 | 7,259 |
| django__django-10097 | ✗ unresolved | $0.75 | 1,771,452 | 22,006 |
| pydata__xarray-2905 | ✗ unresolved | $0.57 | 728,189 | 8,877 |
| sphinx-doc__sphinx-10323 | ✗ unresolved | $0.39 | 411,166 | 7,694 |

## Timing breakdown (per instance, phases overlap across the 8)

| Phase | Time |
|---|---|
| Bootstrap (boot official image + pip-install agent-server) | 42–64s (mostly ~62s) |
| Agent solve (conversation) | 194–439s (median ~4 min) |
| Grading (run FAIL_TO_PASS / PASS_TO_PASS in-sandbox) | ~1–3 min |

Longest pole: `django__django-10097` — 439s solve, 1.77M tokens, still missed (agent thrashing).

## Key findings

1. **The failures were LLM rate limits, not infrastructure.** An earlier run at `workers=8`
   failed 7/8 with `RateLimitError` from Azure. Same 8 instances, same concurrency, only
   change = Foundry TPM raised to 1M → **0 rate-limit failures, all 8 ran clean**. The
   "unstable sandbox infra" theory is disproven.
2. **Cost is ~all input tokens** — 6.21M prompt : 71K completion (~88:1). The agent resends
   growing history each step. **Prompt caching (if the deployment supports it) should cut
   cost 2–5×.**
3. **~$0.56/instance average** ($0.39–$0.75).

## Extrapolation (full SWE-bench_Verified, 500 instances, workers=8)

- Cost ≈ **~$280**
- Wall-clock ≈ **~1.5–2 hrs** (compressible with higher `--workers`; 1M TPM fits ~11 concurrent agents)

## Baseline = 5/8. Next-step targets

- `django-10097` burned 1.77M tokens thrashing and still failed → **stuck-detection** or a
  **verify/critic pass** would cut cost + wall-clock and likely convert some misses.
- This 5/8 is the number to beat when iterating on `build_agent()` / `run_one()`.

---
*Setup + architecture: [SETUP.md](./SETUP.md). Usage: [README.md](./README.md).*
