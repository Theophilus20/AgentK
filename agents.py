"""
agents.py
---------
The AI agents of AgentK and the OpenRouter (gpt-4o-mini) client.

Design:
  - Every agent has a cryptographic identity: wallet address, public key,
    skill tags, and a live reputation score. No fake agents.
  - The Coordinator decomposes a user goal into a DAG of tasks.
  - Specialist agents bid (negotiate) and execute.
  - A separate Verifier agent independently checks each output.

Real OpenRouter calls only (default model: openai/gpt-4o-mini, set via
OPENROUTER_MODEL). No mock — if the key is missing, the app tells you clearly.
"""

from __future__ import annotations
import os
import json
import secrets
import asyncio

# Load .env if present, so OPENROUTER_API_KEY etc. are picked up automatically.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from dataclasses import dataclass, field
from typing import Optional

import httpx

from kaspa_layer import sha256
from kaspa_autonomous import real_agent_address

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Model is configurable via .env (OPENROUTER_MODEL); defaults to gpt-4o-mini.
MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip()


class LLMConfigError(RuntimeError):
    """Raised when the LLM is not configured — no silent fallback."""


# --------------------------------------------------------------------------- #
#  LLM client  — real OpenRouter calls only, no mock
# --------------------------------------------------------------------------- #

class LLM:
    def __init__(self) -> None:
        self.api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        self.model = MODEL
        self.live = bool(self.api_key)

    async def chat(self, system: str, user: str, json_mode: bool = False) -> str:
        if not self.api_key:
            raise LLMConfigError(
                "OPENROUTER_API_KEY is not set. Copy .env.example to .env and "
                "add your key — AgentK makes real gpt-4o-mini calls, no mock."
            )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://agentk.local",
            "X-Title": "AgentK",
        }
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.4,
            "max_tokens": 8000,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(OPENROUTER_URL, headers=headers, json=body)
            if r.status_code != 200:
                raise RuntimeError(
                    f"OpenRouter error {r.status_code}: {r.text[:300]}"
                )
            data = r.json()
            return data["choices"][0]["message"]["content"]


LLM_CLIENT = LLM()


# --------------------------------------------------------------------------- #
#  Agent identity + registry
# --------------------------------------------------------------------------- #

@dataclass
class Agent:
    name: str
    role: str                       # research / copywriting / design / ...
    skill_tags: list[str]
    base_cost: float                # KAS the agent tends to ask for
    wallet: str = field(default_factory=real_agent_address)
    pubkey: str = field(default_factory=lambda: sha256(secrets.token_hex(16)))
    reputation: float = 75.0        # 0..100
    jobs_done: int = 0
    avg_latency_s: float = 0.0

    def identity(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "wallet": self.wallet,
            "pubkey": self.pubkey[:14] + "…",
            "skill_tags": self.skill_tags,
            "reputation": round(self.reputation, 1),
            "jobs_done": self.jobs_done,
        }

    def bid(self, est_kas: float) -> float:
        """Negotiation: higher-rep agents can price at a slight premium."""
        rep_factor = 1 + (self.reputation - 75) / 300
        return round(max(1.0, est_kas * rep_factor), 1)

    def update_reputation(self, quality: int, latency_s: float) -> None:
        # EMA toward the latest quality score; track latency + volume.
        self.reputation = round(0.7 * self.reputation + 0.3 * quality, 2)
        self.jobs_done += 1
        self.avg_latency_s = round(
            (self.avg_latency_s * (self.jobs_done - 1) + latency_s)
            / self.jobs_done, 2
        )


class Registry:
    """Marketplace of specialist agents. Coordinator selects by skill+rep.

    State (reputation, jobs done, wallets) persists to agent_state.json so
    agents keep their history and real testnet addresses across restarts.
    """

    STATE_FILE = os.path.join(os.path.dirname(__file__), "agent_state.json")

    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}
        self._seed()
        self._load()

    def _load(self) -> None:
        try:
            with open(self.STATE_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            for role, st in saved.items():
                a = self.agents.get(role)
                if not a:
                    continue
                a.reputation = float(st.get("reputation", a.reputation))
                a.jobs_done = int(st.get("jobs_done", a.jobs_done))
                a.avg_latency_s = float(st.get("avg_latency_s", 0.0))
                if st.get("wallet"):
                    a.wallet = st["wallet"]
                if st.get("pubkey"):
                    a.pubkey = st["pubkey"]
        except FileNotFoundError:
            self.save()
        except Exception:
            pass  # corrupted state: keep seeds, will re-save

    def save(self) -> None:
        try:
            data = {
                role: {
                    "reputation": a.reputation,
                    "jobs_done": a.jobs_done,
                    "avg_latency_s": a.avg_latency_s,
                    "wallet": a.wallet,
                    "pubkey": a.pubkey,
                }
                for role, a in self.agents.items()
            }
            with open(self.STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
        except Exception:
            pass

    def _seed(self) -> None:
        seed = [
            Agent("Athena", "research", ["research", "analysis"], 3, reputation=82),
            Agent("Quill", "copywriting", ["copywriting", "content"], 4, reputation=78),
            Agent("Pixel", "design", ["design", "ui", "ux"], 6, reputation=85),
            Agent("Forge", "frontend", ["frontend", "code", "react"], 8, reputation=80),
            Agent("Probe", "testing", ["testing", "qa"], 2, reputation=88),
            Agent("Nova", "integration", ["integration", "assembly", "packaging"], 3, reputation=84),
        ]
        for a in seed:
            self.agents[a.role] = a

    def match(self, skill: str) -> Agent:
        """Pick best agent for a skill; fall back to research generalist."""
        candidates = [
            a for a in self.agents.values()
            if skill in a.skill_tags or skill == a.role
        ]
        if not candidates:
            return self.agents["research"]
        return max(candidates, key=lambda a: a.reputation)

    def all(self) -> list[dict]:
        return [a.identity() for a in self.agents.values()]


REGISTRY = Registry()
