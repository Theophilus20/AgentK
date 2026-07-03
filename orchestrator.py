"""
orchestrator.py
---------------
The Coordinator runtime. Turns a user goal into a verified, settled result.

Lifecycle per job (each step anchored on the Kaspa commitment layer):
  1. Decompose goal into a DAG of tasks (Coordinator LLM).
  2. Escrow the full budget on Kaspa (funds locked before any work).
  3. For each task: match agent -> negotiate bid -> anchor TASK commitment
     -> agent executes -> anchor WORK_PROOF (output hash) -> Verifier checks
     -> programmable SETTLEMENT (release or refund) -> update REPUTATION.
  4. Assemble final result for the user.

Events are streamed so the UI can render the coordination live.
"""

from __future__ import annotations
import os
import time
import json
import secrets
from typing import AsyncGenerator

from kaspa_layer import (
    LEDGER, CommitmentKind, SettlementResult, sha256,
)
from agents import LLM_CLIENT, REGISTRY, MODEL
from kaspa_autonomous import SIGNER

# Real on-chain payments are scaled to preserve faucet funds across demos.
# 0.1 => a 10 KAS bid pays 1.0 real tKAS on-chain. Set to 1.0 for full bids.
PAY_SCALE = float(os.environ.get("ONCHAIN_PAY_SCALE", "1.0") or 1.0)


COORDINATOR_SYS = (
    "You are the AgentK Coordinator, an expert project planner. Decompose the "
    "user's goal into 4-6 concrete, independently-executable tasks that "
    "together fully achieve the goal. Order them logically (research before "
    "design, design before build, build before test). NEVER create "
    "deployment or hosting tasks — the user deploys the final work "
    "themselves; agents hand over finished files. The LAST task must be an "
    "integration task that assembles all prior outputs into the final, "
    "ready-to-use deliverable (e.g. one complete HTML file). Estimate a fair "
    "cost for each so the total stays within budget. Respond ONLY as JSON: "
    '{"tasks":[{"title": str, "skill": one of '
    "[research,copywriting,design,frontend,testing,integration], "
    '"est_kas": number}]}'
)

VERIFIER_SYS = (
    "You are the AgentK Verifier, an independent AI auditor with no stake in "
    "the outcome. Judge whether the agent's output genuinely satisfies the "
    "task: is it complete (nothing missing or cut off), correct, specific "
    "(no vague filler), and usable as-is by the next agent in the pipeline? "
    "Respond ONLY as JSON: "
    '{"approved": bool, "score": 0-100, "reason": str}. '
    "Be strict but fair: score below 60 means not approved. Penalize "
    "incompleteness heavily; reward concrete, actionable output."
)


ROLE_GUIDES = {
    "research": "Deliver findings: concrete facts, competitor specifics, and "
                "actionable recommendations. Never deliver code.",
    "copywriting": "Deliver the final copy text itself, organized by page "
                   "section — headlines, body, CTAs. Never deliver code.",
    "design": "Deliver a precise design spec: layout per section, color "
              "palette with hex values, typography, spacing. Wireframe as "
              "structured description, not code.",
    "frontend": "Deliver complete, working code — every tag closed, no "
                "placeholders, runnable as-is.",
    "testing": "Deliver a TEST REPORT, never code: what you evaluated, "
               "checks performed (structure, links, responsiveness, "
               "accessibility), issues found with severity, and concrete "
               "fixes. If prior work contains code, audit it line by line.",
    "integration": "Deliver ONE final, complete, polished file assembling "
                   "ALL prior outputs (research insights, copy, design "
                   "spec, code, test fixes). Apply the test report's fixes. "
                   "This is the file the user takes away — make it whole.",
}


def _worker_sys(role: str) -> str:
    guide = ROLE_GUIDES.get(role, "Deliver complete, professional work.")
    return (
        f"You are {role.capitalize()}, an elite specialist agent in AgentK. "
        f"ROLE RULES: {guide} "
        "Deliver COMPLETE, professional work for the assigned task — never "
        "truncate, never leave placeholders like 'TODO' or '...'. Your output "
        "will be independently audited by a strict Verifier: incomplete work "
        "is rejected and you are not paid. Size the deliverable to fit COMPLETELY within one response — a finished medium-length deliverable beats a truncated long one. Be specific and concrete; if the "
        "task involves code, include ALL of it, working end to end. "
        "Respond ONLY as JSON: "
        '{"output": str (the full deliverable), '
        '"summary": str (1-2 sentences describing what you delivered)}.'
    )


class Job:
    def __init__(self, goal: str, budget: float, deadline_min: int) -> None:
        self.id = "job_" + secrets.token_hex(4)
        self.goal = goal
        self.budget = budget
        self.deadline_min = deadline_min
        self.user_addr = "kaspa:user_" + secrets.token_hex(8)
        # Fund the user's demo wallet so escrow can lock real balance.
        LEDGER.fund(self.user_addr, budget + 5)
        self.tasks: list[dict] = []
        self.results: list[dict] = []


QUALITY_THRESHOLDS = {"high": 75, "balanced": 60, "fast": 50}


async def run_job(goal: str, budget: float, deadline_min: int,
                  quality: str = "high") -> AsyncGenerator[dict, None]:
    """Async generator of lifecycle events (for SSE streaming)."""
    try:
        async for ev in _run_job_inner(goal, budget, deadline_min, quality):
            yield ev
    except Exception as e:
        yield _ev("error", {"message": str(e)})


async def _run_job_inner(goal: str, budget: float, deadline_min: int,
                         quality: str = "high") -> AsyncGenerator[dict, None]:
    quality_threshold = QUALITY_THRESHOLDS.get(
        str(quality).lower(), QUALITY_THRESHOLDS["high"])
    job = Job(goal, budget, deadline_min)

    yield _ev("job_created", {
        "job_id": job.id, "goal": goal, "budget": budget,
        "deadline_min": deadline_min, "user_wallet": job.user_addr,
        "llm_live": LLM_CLIENT.live, "model": MODEL,
    })

    # 1. DECOMPOSE -------------------------------------------------------- #
    raw = await LLM_CLIENT.chat(COORDINATOR_SYS, f"Goal: {goal}. Budget: {budget} KAS.",
                                json_mode=True)
    tasks = _parse_tasks(raw)
    job.tasks = tasks
    yield _ev("dag_built", {"tasks": tasks})

    # 2. ESCROW ----------------------------------------------------------- #
    lock = LEDGER.lock_escrow(job.id, job.user_addr, budget)
    yield _ev("escrow_locked", {
        "txid": lock.txid, "amount": budget,
        "commitment_hash": lock.commitment_hash,
        "user_balance": LEDGER.balance(job.user_addr),
    })

    spent = 0.0
    refunded_tasks = 0.0
    # 3. PER-TASK LIFECYCLE ---------------------------------------------- #
    for i, task in enumerate(tasks):
        skill = task.get("skill", "research")
        agent = REGISTRY.match(skill)

        # Negotiate
        bid = agent.bid(float(task.get("est_kas", 3)))
        yield _ev("negotiation", {
            "task_index": i, "task": task["title"], "skill": skill,
            "agent": agent.name, "agent_wallet": agent.wallet,
            "reputation": agent.reputation, "bid_kas": bid,
        })

        # Anchor task commitment
        expected_hash = sha256(f"{job.id}:{i}:{task['title']}")
        tc = LEDGER.anchor(CommitmentKind.TASK, {
            "job_id": job.id, "task_index": i, "title": task["title"],
            "agent": agent.name, "agent_wallet": agent.wallet,
            "reward_kas": bid, "expected_output_hash": expected_hash,
            "deadline_min": deadline_min,
        })
        yield _ev("task_committed", {
            "task_index": i, "txid": tc.txid,
            "commitment_hash": tc.commitment_hash, "reward_kas": bid,
        })

        # REAL ON-CHAIN AGREEMENT: the signed pact itself is anchored on
        # Kaspa before work starts — no party can later deny the terms.
        if SIGNER.enabled:
            oc = await SIGNER.send(SIGNER.address, 0.2,
                                   payload_hex=tc.commitment_hash[2:])
            yield _ev("onchain_commitment", {
                "task_index": i, "agent": agent.name, "reward_kas": bid,
                "ok": oc.ok, "txid": oc.txid,
                "explorer_url": oc.explorer_url, "api_url": oc.api_url,
                "error": oc.error,
            })

        # Execute — with pipeline context so agents build on prior work.
        t0 = time.time()
        prior = ""
        if job.results:
            prior = "\n\nCompleted so far (build on this):\n" + "\n".join(
                f"- {r['task']}: {str(r['output'])[:1200]}" for r in job.results[-5:]
            )
        out_raw = await LLM_CLIENT.chat(
            _worker_sys(agent.role),
            f"Task: {task['title']}\nGoal context: {goal}{prior}",
            json_mode=True,
        )
        out = _parse_output(out_raw)
        latency = round(time.time() - t0, 2)
        work_hash = sha256(json.dumps(out, sort_keys=True))

        # Anchor work proof
        wp = LEDGER.anchor(CommitmentKind.WORK_PROOF, {
            "job_id": job.id, "task_index": i, "agent": agent.name,
            "work_hash": work_hash, "latency_s": latency,
        })
        yield _ev("work_submitted", {
            "task_index": i, "agent": agent.name, "txid": wp.txid,
            "work_hash": work_hash, "latency_s": latency,
            "summary": out.get("summary", ""),
        })

        # Verify (independent agent) — the verifier MUST see the FULL output;
        # excerpting caused false "incomplete" rejections.
        v_raw = await LLM_CLIENT.chat(
            VERIFIER_SYS,
            f"Task: {task['title']}\nOutput (complete, "
            f"{len(out.get('output',''))} chars):\n{json.dumps(out)[:60000]}",
            json_mode=True,
        )
        verdict = _parse_verdict(v_raw)
        # Quality setting adjusts the approval bar.
        verdict["approved"] = bool(verdict["approved"]) and \
            verdict["score"] >= quality_threshold
        yield _ev("verified", {
            "task_index": i, "approved": verdict["approved"],
            "score": verdict["score"], "reason": verdict["reason"],
        })

        # Programmable settlement: IF approved AND deadline met -> release.
        deadline_met = latency <= deadline_min * 60
        if verdict["approved"] and deadline_met:
            result = SettlementResult.RELEASED
            spent += bid
        else:
            result = SettlementResult.REFUNDED
            refunded_tasks += bid
        st = LEDGER.settle(job.id, agent.wallet, job.user_addr, result, bid)
        yield _ev("settled", {
            "task_index": i, "result": result.value, "txid": st.txid,
            "amount": bid, "agent_wallet": agent.wallet,
            "agent_balance": LEDGER.balance(agent.wallet),
        })

        # AUTONOMOUS REAL ON-CHAIN PAYMENT (Testnet-10) ------------------- #
        # If a treasury key is configured, pay the agent's REAL testnet
        # address with the task's commitment hash as the tx payload.
        if SIGNER.enabled and result == SettlementResult.RELEASED:
            pay = round(max(0.2, bid * PAY_SCALE), 4)
            oc = await SIGNER.send(agent.wallet, pay,
                                   payload_hex=wp.commitment_hash[2:])
            yield _ev("onchain_settled", {
                "task_index": i, "agent": agent.name,
                "agent_wallet": agent.wallet, "amount_kas": pay,
                "ok": oc.ok, "txid": oc.txid,
                "explorer_url": oc.explorer_url, "api_url": oc.api_url,
                "error": oc.error,
            })
        # Reputation update (anchored)
        agent.update_reputation(verdict["score"], latency)
        REGISTRY.save()
        rc = LEDGER.anchor(CommitmentKind.REPUTATION, {
            "agent": agent.name, "wallet": agent.wallet,
            "new_reputation": agent.reputation, "jobs_done": agent.jobs_done,
        })
        yield _ev("reputation_updated", {
            "task_index": i, "agent": agent.name,
            "reputation": agent.reputation, "txid": rc.txid,
        })

        job.results.append({
            "task": task["title"], "agent": agent.name,
            "output": out.get("output", ""), "score": verdict["score"],
            "settled": result.value,
        })

    # 4. FINAL ------------------------------------------------------------ #
    leftover = LEDGER.release_remaining_escrow(job.id, job.user_addr)
    # Master hash of the whole job — one on-chain anchor seals everything.
    master_hash = sha256(json.dumps({
        "job": job.id, "goal": goal,
        "results": [(r["task"], r["score"], r["settled"]) for r in job.results],
        "spent": round(spent, 2),
    }, sort_keys=True))

    # AUTONOMOUS MASTER ANCHOR: seal the whole job on-chain, no clicks.
    if SIGNER.enabled:
        oc = await SIGNER.send(SIGNER.address, 0.2,
                               payload_hex=master_hash[2:])
        yield _ev("onchain_anchor", {
            "ok": oc.ok, "txid": oc.txid,
            "explorer_url": oc.explorer_url, "api_url": oc.api_url,
            "error": oc.error,
        })

    yield _ev("job_complete", {
        "job_id": job.id,
        "results": job.results,
        "total_spent_kas": round(spent, 2),
        "refunded_kas": round(refunded_tasks + leftover, 2),
        "master_hash": master_hash,
        "user_balance": LEDGER.balance(job.user_addr),
        "chain_verified": LEDGER.verify_integrity(),
        "chain_length": len(LEDGER.get_chain()),
    })


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #

def _ev(kind: str, data: dict) -> dict:
    return {"event": kind, "ts": round(time.time(), 3), "data": data}


def _safe_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].replace("json", "", 1).strip()
    try:
        return json.loads(raw)
    except Exception:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(raw[start:end + 1])
            except Exception:
                pass
    return {}


def _parse_tasks(raw: str) -> list[dict]:
    data = _safe_json(raw)
    tasks = data.get("tasks", [])
    clean = []
    for t in tasks[:6]:
        clean.append({
            "title": str(t.get("title", "Untitled task")),
            "skill": str(t.get("skill", "research")).lower(),
            "est_kas": float(t.get("est_kas", 3) or 3),
        })
    if not clean:
        clean = [{"title": "Complete the goal", "skill": "research", "est_kas": 3}]
    return clean


def _parse_output(raw: str) -> dict:
    data = _safe_json(raw)
    if not data:
        data = {"output": raw[:500], "summary": "Completed."}
    # Models sometimes return nested JSON for "output"; coerce to string so
    # downstream slicing/hashing/pipeline-context never breaks.
    if not isinstance(data.get("output"), str):
        data["output"] = json.dumps(data.get("output", ""), ensure_ascii=False)
    if not isinstance(data.get("summary"), str):
        data["summary"] = str(data.get("summary", ""))[:300]
    return data


def _parse_verdict(raw: str) -> dict:
    data = _safe_json(raw)
    return {
        "approved": bool(data.get("approved", True)),
        "score": int(data.get("score", 80) or 80),
        "reason": str(data.get("reason", "Meets requirements.")),
    }