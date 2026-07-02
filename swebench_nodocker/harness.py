#!/usr/bin/env python3
"""No-Docker SWE-bench harness on the OpenHands remote runtime.

Runs real SWE-bench_Verified instances end-to-end WITHOUT a local Docker daemon and
WITHOUT building/pushing any custom images:

  1. Boot the OFFICIAL swebench base image (docker.io/swebench/sweb.eval.x86_64.<inst>)
     on the remote runtime (runtime.eval.all-hands.dev).
  2. Bootstrap the OpenHands agent-server from PyPI at container start (command-as-array).
  3. Run the OpenHands agent (any LiteLLM-compatible model) against the repo at /testbed.
  4. Grade IN THE SAME SANDBOX using the official `swebench` eval script + parser.

This is the fast iteration loop for improving the agentic system: edit the agent
config / orchestration below, rerun on a few instances, read resolved-rate. No image
rebuilds (see SETUP.md "iteration tiers").

Env required:
  RUNTIME_API_KEY   runtime.eval.all-hands.dev key
  LLM_MODEL         e.g. azure_ai/DeepSeek-V4-Pro
  LLM_BASE_URL      e.g. https://glia-foundry-openhands.services.ai.azure.com/models
  LLM_API_VERSION   e.g. 2024-05-01-preview   (Azure Foundry; optional otherwise)
  LLM_API_KEY       model key
Optional:
  RUNTIME_API_URL   (default https://runtime.eval.all-hands.dev)
  AS_VER            agent-server/tools version to install (default 1.27.0; match your SDK)

Usage:
  uv run python swebench_nodocker/harness.py --instances django__django-11099 --workers 1
"""

import argparse
import base64
import json
import os
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
from jinja2 import Template
from pydantic import SecretStr
from swebench.harness.grading import get_eval_report
from swebench.harness.test_spec.test_spec import make_test_spec

from benchmarks.swebench.build_images import get_official_docker_image
from benchmarks.utils.dataset import get_dataset
from openhands.sdk import LLM, Conversation, get_logger
from openhands.sdk.workspace import RemoteWorkspace
from openhands.tools.preset.default import get_default_agent


logger = get_logger("swe_nodocker")

API = os.environ.get("RUNTIME_API_URL", "https://runtime.eval.all-hands.dev").rstrip(
    "/"
)
KEY = os.environ["RUNTIME_API_KEY"]
AS_VER = os.environ.get("AS_VER", "1.27.0")
HDR = {"X-API-Key": KEY}
REPO_ROOT = Path(__file__).resolve().parents[1]

# --- Bootstrap script run inside the official swebench image ---------------------------
# KEY LEARNINGS baked in here:
#  * command MUST be a JSON array (the runtime naive-splits a string command).
#  * uv's cache step OOM/crashes the pod -> install with --no-cache + UV_NO_CACHE=1.
#  * readiness probe needs an agent-server HTTP endpoint (/ready|/server_info), not just
#    a bound port -> the debug fallback returns 200 on ALL paths so failures are inspectable.
BOOTSTRAP = f"""
export HOME=/root PATH=/root/.local/bin:$PATH UV_NO_CACHE=1
cd /tmp
cat > /tmp/srv.py <<'PYEOF'
import http.server, socketserver
class Hd(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/boot.log':
            return super().do_GET()
        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
    def log_message(self, *a): pass
socketserver.TCPServer.allow_reuse_address = True
socketserver.TCPServer(('', 60000), Hd).serve_forever()
PYEOF
{{
  set -x
  (curl -LsSf https://astral.sh/uv/install.sh | sh) || (wget -qO- https://astral.sh/uv/install.sh | sh)
  export PATH=/root/.local/bin:$PATH
  uv venv --python 3.12 /opt/as
  uv pip install --no-cache --python /opt/as/bin/python "openhands-agent-server=={AS_VER}" "openhands-tools=={AS_VER}"
}} > /tmp/boot.log 2>&1
echo "=== starting agent-server ===" >> /tmp/boot.log
/opt/as/bin/python -m openhands.agent_server --port 60000 >> /tmp/boot.log 2>&1 &
SP=$!
sleep 20
if kill -0 $SP 2>/dev/null; then wait $SP; else echo SERVER_DIED_EARLY >> /tmp/boot.log; exec python3 /tmp/srv.py; fi
"""

PROMPT = Template((REPO_ROOT / "benchmarks/swebench/prompts/default.j2").read_text())


def start_runtime(image, session_id, wait=900):
    payload = {
        "image": image,
        "command": ["bash", "-lc", BOOTSTRAP],
        "working_dir": "/",
        "environment": {},
        "session_id": session_id,
        "run_as_user": 0,
        "fs_group": 0,
        "image_pull_policy": "IfNotPresent",
        "runtime_class": "sysbox-runc",
    }
    c = httpx.Client(timeout=90)
    d = c.post(f"{API}/start", json=payload, headers=HDR).json()
    rid = d.get("runtime_id") or d.get("id")
    url = d.get("url")
    t0 = time.time()
    ps = None
    while time.time() - t0 < wait:
        time.sleep(10)
        ps = c.get(f"{API}/sessions/{session_id}", headers=HDR).json().get("pod_status")
        if ps == "ready":
            try:
                si = httpx.get(f"{url.rstrip('/')}/server_info", timeout=10)
                if (
                    si.status_code == 200
                    and isinstance(si.json(), dict)
                    and si.text.strip() != "ok"
                ):
                    logger.info(
                        f"[{session_id}] agent-server ready in {int(time.time() - t0)}s"
                    )
                    return rid, url, d.get("session_api_key")
            except Exception:
                pass
            try:
                boot = httpx.get(f"{url.rstrip('/')}/boot.log", timeout=10).text[-1200:]
            except Exception:
                boot = "(no boot.log)"
            raise RuntimeError(f"agent-server bootstrap failed; boot.log tail:\n{boot}")
        if ps == "crashloopbackoff":
            raise RuntimeError("pod crashloopbackoff during bootstrap")
    raise RuntimeError(f"runtime not ready after {wait}s (last {ps})")


def stop_runtime(rid):
    try:
        httpx.post(f"{API}/stop", json={"runtime_id": rid}, headers=HDR, timeout=30)
    except Exception:
        pass


def make_llm(usage_id="agent"):
    return LLM(
        usage_id=usage_id,
        model=os.environ["LLM_MODEL"],
        base_url=os.environ["LLM_BASE_URL"],
        api_version=os.environ.get("LLM_API_VERSION"),
        api_key=SecretStr(os.environ["LLM_API_KEY"]),
        # Ride out shared-endpoint rate-limit spikes under parallel runs.
        num_retries=int(os.environ.get("LLM_NUM_RETRIES", "10")),
        retry_max_wait=int(os.environ.get("LLM_RETRY_MAX_WAIT", "120")),
    )


def instruction(row):
    inst = type("I", (), {})()
    inst.repo_path = "/testbed"
    inst.problem_statement = row["problem_statement"]
    inst.base_commit = row["base_commit"]
    return PROMPT.render(instance=inst)


def build_agent():
    """<<< EDIT HERE to build a better agent (prompt/tools/critic/MAS). Tier-1/2 changes
    ship to the sandbox per-run with no image rebuild. >>>"""
    return get_default_agent(llm=make_llm(), cli_mode=True)


def run_one(row, max_iter=100, grade_timeout=1800):
    iid = row["instance_id"]
    base = row["base_commit"]
    session_id = f"swe-{iid.replace('__', '-')[:40]}-{uuid.uuid4().hex[:6]}"
    rid = None
    try:
        rid, url, skey = start_runtime(get_official_docker_image(iid), session_id)
        ws = RemoteWorkspace(host=url, api_key=skey, working_dir="/testbed")
        ws.execute_command(
            f"cd /testbed && git reset --hard {base} && git clean -fdx", timeout=180
        )
        convo = Conversation(
            agent=build_agent(),
            workspace=ws,
            max_iteration_per_run=max_iter,
            visualizer=None,
        )
        convo.send_message(instruction(row))
        convo.run()
        # capture real usage metrics
        cost, tokens = None, {}
        try:
            m = convo.conversation_stats.get_combined_metrics()
            cost = getattr(m, "accumulated_cost", None)
            tu = getattr(m, "accumulated_token_usage", None)
            if tu is not None:
                tokens = {"prompt": getattr(tu, "prompt_tokens", None),
                          "completion": getattr(tu, "completion_tokens", None)}
        except Exception:
            pass
        ws.execute_command("cd /testbed && git add -A", timeout=60)
        patch = ws.execute_command(
            f"cd /testbed && git diff --cached {base}", timeout=120
        ).stdout
        # ---- grade in-sandbox ----
        ts = make_test_spec(row)
        b64 = base64.b64encode(ts.eval_script.encode()).decode()
        ws.execute_command(f"echo {b64} | base64 -d > /tmp/eval.sh", timeout=60)
        log = (
            ws.execute_command("bash /tmp/eval.sh 2>&1", timeout=grade_timeout).stdout
            or ""
        )
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(log)
            logpath = f.name
        pred = {
            "instance_id": iid,
            "model_name_or_path": "openhands-nodocker",
            "model_patch": patch,
        }
        try:
            report = get_eval_report(ts, pred, logpath, include_tests_status=True)
            resolved = bool(report.get(iid, {}).get("resolved", False))
        except Exception as e:
            report = {"grade_error": str(e)}
            resolved = False
        logger.info(
            f"[{iid}] resolved={resolved} patch_len={len(patch)} cost={cost} tokens={tokens}"
        )
        return {
            "instance_id": iid,
            "resolved": resolved,
            "patch": patch,
            "error": None,
            "report": report.get(iid, report),
            "cost": cost,
            "tokens": tokens,
        }
    except Exception as e:
        logger.warning(f"[{iid}] ERROR {type(e).__name__}: {e}")
        return {
            "instance_id": iid,
            "resolved": False,
            "patch": "",
            "error": f"{type(e).__name__}: {e}",
        }
    finally:
        if rid:
            stop_runtime(rid)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instances", nargs="+", required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-iter", type=int, default=100)
    ap.add_argument("--dataset", default="princeton-nlp/SWE-bench_Verified")
    ap.add_argument("--split", default="test")
    ap.add_argument(
        "--out", default=str(REPO_ROOT / "swebench_nodocker" / "results.jsonl")
    )
    args = ap.parse_args()
    df = get_dataset(
        dataset_name=args.dataset,
        split=args.split,
        eval_limit=None,
        selected_instances_file=None,
    )
    rows = [df[df["instance_id"] == i].iloc[0].to_dict() for i in args.instances]
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_one, r, args.max_iter): r["instance_id"] for r in rows}
        for fu in as_completed(futs):
            results.append(fu.result())
    with open(args.out, "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    results.sort(key=lambda x: x["instance_id"])
    print("\n==== RESULTS ====")
    for r in results:
        s = (
            "RESOLVED"
            if r["resolved"]
            else ("ERROR: " + (r["error"] or "")[:50] if r["error"] else "unresolved")
        )
        c = r.get("cost")
        print(f"  {r['instance_id']:<40} {s:<12} cost={c}")
    total_cost = sum((r.get("cost") or 0) for r in results)
    total_prompt = sum((r.get("tokens") or {}).get("prompt") or 0 for r in results)
    total_completion = sum((r.get("tokens") or {}).get("completion") or 0 for r in results)
    print(
        f"RESOLVED {sum(r['resolved'] for r in results)}/{len(results)}  "
        f"| cost=${total_cost:.4f}  prompt_tok={total_prompt:,} completion_tok={total_completion:,}  -> {args.out}"
    )


if __name__ == "__main__":
    main()
