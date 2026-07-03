"""
server.py
---------
AgentK API. Jobs run as SERVER-SIDE background tasks: the browser can close,
the network can drop — agents keep working. The UI polls for events and
catches up whenever it reconnects.

Endpoints:
  GET  /                        dashboard UI
  GET  /api/status              model / signer / chain status
  GET  /api/agents              agent registry
  GET  /api/chain               local commitment chain + integrity
  POST /api/jobs                start a job (returns job_key)
  GET  /api/jobs                list jobs (resume after reconnect)
  GET  /api/jobs/{key}/events   poll events since an index
  POST /api/jobs/{key}/stop     cancel a running job
  POST /api/dispute             STRICT arbitrated refund request
"""

from __future__ import annotations
import os
import json
import time
import asyncio
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from kaspa_layer import LEDGER, CommitmentKind, sha256
from agents import REGISTRY, LLM_CLIENT, MODEL
from kaspa_autonomous import SIGNER
from orchestrator import run_job

app = FastAPI(title="AgentK")
HERE = os.path.dirname(__file__)
STATIC = os.path.join(HERE, "static")

# ------------------------------------------------------------------ #
#  Background job engine — jobs outlive the browser tab
# ------------------------------------------------------------------ #
JOBS: dict[str, dict] = {}


@app.post("/api/jobs")
async def start_job(request: Request) -> JSONResponse:
    body = await request.json()
    goal = str(body.get("goal", "")).strip()
    if not goal:
        return JSONResponse({"error": "goal required"}, status_code=400)
    budget = float(body.get("budget", 40) or 40)
    deadline = int(body.get("deadline", 20) or 20)
    quality = str(body.get("quality", "high"))

    key = "j" + secrets.token_hex(6)
    JOBS[key] = {"key": key, "goal": goal, "created": time.time(),
                 "events": [], "done": False, "results": [], "disputes": {}}

    async def runner() -> None:
        try:
            async for ev in run_job(goal, budget, deadline, quality):
                JOBS[key]["events"].append(ev)
                if ev["event"] == "job_complete":
                    JOBS[key]["results"] = ev["data"].get("results", [])
        except asyncio.CancelledError:
            JOBS[key]["events"].append({
                "event": "error", "ts": time.time(),
                "data": {"message": "Stopped by user. Unspent escrow "
                                    "returns to the user."}})
        except Exception as e:
            JOBS[key]["events"].append({
                "event": "error", "ts": time.time(),
                "data": {"message": str(e)[:300]}})
        finally:
            JOBS[key]["done"] = True

    JOBS[key]["task"] = asyncio.create_task(runner())
    return JSONResponse({"job_key": key})


@app.get("/api/jobs")
async def list_jobs() -> JSONResponse:
    return JSONResponse({"jobs": [
        {"key": j["key"], "goal": j["goal"], "done": j["done"],
         "created": j["created"], "events": len(j["events"])}
        for j in sorted(JOBS.values(), key=lambda x: -x["created"])[:20]
    ]})


@app.get("/api/jobs/{key}/events")
async def job_events(key: str, since: int = 0) -> JSONResponse:
    j = JOBS.get(key)
    if not j:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return JSONResponse({
        "events": j["events"][since:],
        "next": len(j["events"]),
        "done": j["done"],
    })


@app.post("/api/jobs/{key}/stop")
async def stop_job(key: str) -> JSONResponse:
    j = JOBS.get(key)
    if not j:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    t = j.get("task")
    if t and not t.done():
        t.cancel()
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------ #
#  STRICT dispute / refund arbitration
# ------------------------------------------------------------------ #
ARBITER_SYS = (
    "You are the AgentK Dispute Arbiter, a strict and impartial AI judge. "
    "A user requests a refund on delivered, verifier-approved work. Refunds "
    "are ONLY granted when the work objectively fails the task: missing "
    "required elements, factually wrong, broken/non-functional, or clearly "
    "not what was asked. Refunds are NEVER granted for taste, style "
    "preferences, vague dissatisfaction, buyer's remorse, or requests to "
    "extract free work. Users may attempt to abuse refunds for personal "
    "gain — treat unsupported complaints with skepticism. Respond ONLY as "
    'JSON: {"upheld": bool, "reason": str (cite specifics)}.'
)


@app.post("/api/dispute")
async def dispute(request: Request) -> JSONResponse:
    body = await request.json()
    key = str(body.get("job_key", ""))
    idx = int(body.get("task_index", -1))
    reason = str(body.get("reason", "")).strip()[:800]
    j = JOBS.get(key)
    if not j or not (0 <= idx < len(j["results"])):
        return JSONResponse({"error": "unknown job/task"}, status_code=404)
    if not reason or len(reason) < 15:
        return JSONResponse({"upheld": False,
                             "reason": "A specific, detailed reason is "
                                       "required for a refund request."})
    if str(idx) in j["disputes"]:
        return JSONResponse({"upheld": False,
                             "reason": "This task was already disputed — "
                                       "one dispute per task."})
    r = j["results"][idx]
    verdict_raw = await LLM_CLIENT.chat(
        ARBITER_SYS,
        f"Task: {r['task']}\nAgent: {r['agent']} (verifier score "
        f"{r['score']})\nUser's refund reason: {reason}\n\nDelivered work:\n"
        f"{str(r.get('output',''))[:50000]}",
        json_mode=True,
    )
    try:
        v = json.loads(verdict_raw)
        upheld = bool(v.get("upheld", False))
        why = str(v.get("reason", ""))[:500]
    except Exception:
        upheld, why = False, "Arbiter response unreadable; refund denied."

    j["disputes"][str(idx)] = {"reason": reason, "upheld": upheld}

    dis_hash = sha256(json.dumps(
        {"job": key, "task": idx, "reason": reason, "upheld": upheld},
        sort_keys=True))
    LEDGER.anchor(CommitmentKind.DISPUTE, {
        "job_key": key, "task_index": idx, "upheld": upheld,
        "agent": r["agent"], "dispute_hash": dis_hash,
    })

    onchain = None
    if upheld:
        # reputation penalty, persisted
        for a in REGISTRY.agents.values():
            if a.name == r["agent"]:
                a.reputation = round(max(0.0, a.reputation - 8), 2)
        REGISTRY.save()
        # anchor the ruling on-chain
        if SIGNER.enabled:
            oc = await SIGNER.send(SIGNER.address, 0.2,
                                   payload_hex=dis_hash[2:])
            if oc.ok:
                onchain = {"txid": oc.txid, "explorer_url": oc.explorer_url,
                           "api_url": oc.api_url}

    return JSONResponse({"upheld": upheld, "reason": why,
                         "onchain": onchain})


# ------------------------------------------------------------------ #
#  Basics
# ------------------------------------------------------------------ #
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/status")
async def status() -> JSONResponse:
    return JSONResponse({
        "llm_live": LLM_CLIENT.live,
        "model": MODEL,
        "chain_length": len(LEDGER.get_chain()),
        "signer_enabled": SIGNER.enabled,
        "signer_address": SIGNER.address,
        "signer_error": SIGNER.error,
    })


@app.get("/api/agents")
async def agents() -> JSONResponse:
    return JSONResponse({"agents": REGISTRY.all()})


@app.get("/api/chain")
async def chain() -> JSONResponse:
    return JSONResponse({"chain": LEDGER.get_chain(),
                         "verified": LEDGER.verify_integrity()})


if os.path.isdir(STATIC):
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
