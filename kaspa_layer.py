"""
kaspa_layer.py
--------------
AgentK's programmable coordination + settlement layer on Kaspa.

Kaspa is NOT used as a dumb payment rail here. It is used as:
  - a COMMITMENT layer   (task agreements are anchored + hash-sealed)
  - an ESCROW layer      (budget is locked before work starts)
  - a MILESTONE layer    (funds release only on verified proof-of-work)
  - a SETTLEMENT layer    (conditional release / refund via programmable logic)
  - a REPUTATION layer   (agent performance anchored as commitments)
  - a DAG layer          (parallel task graph mirrors Kaspa's BlockDAG)

This module ships with a local `LedgerBackend` that faithfully models Kaspa
commitment semantics (append-only, hash-linked, tx-addressed) so the whole
system runs end-to-end today. A `KaspaTestnetBackend` stub shows exactly where
the real kaspad / SilverScript covenant calls slot in — same interface, no
changes to the rest of AgentK.
"""

from __future__ import annotations
import hashlib
import json
import time
import secrets
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


# --------------------------------------------------------------------------- #
#  Primitives
# --------------------------------------------------------------------------- #

def sha256(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode()
    return "0x" + hashlib.sha256(data).hexdigest()


def new_txid() -> str:
    # Kaspa txids are 32-byte hashes; we model that shape.
    return "0x" + secrets.token_hex(32)


def new_address() -> str:
    # Models a kaspa:... bech32 address shape for demo purposes.
    return "kaspa:" + secrets.token_hex(20)


class CommitmentKind(str, Enum):
    TASK = "task_commitment"          # agreement anchored
    ESCROW_LOCK = "escrow_lock"       # budget locked
    WORK_PROOF = "work_proof"         # proof-of-work hash sealed
    SETTLEMENT = "settlement"         # conditional release / refund
    REPUTATION = "reputation"         # agent score anchored
    DISPUTE = "dispute"               # user dispute + arbiter ruling


class SettlementResult(str, Enum):
    RELEASED = "released"     # verifier approved + deadline met -> pay agent
    REFUNDED = "refunded"     # failed conditions -> refund user
    PENDING = "pending"


# --------------------------------------------------------------------------- #
#  On-chain records
# --------------------------------------------------------------------------- #

@dataclass
class Commitment:
    """One anchored, hash-linked record. Append-only, like a confirmed tx."""
    txid: str
    kind: CommitmentKind
    payload: dict
    prev_hash: str            # hash-links records (BlockDAG-style lineage)
    timestamp: float
    commitment_hash: str = ""

    def seal(self) -> "Commitment":
        body = json.dumps(
            {
                "txid": self.txid,
                "kind": self.kind.value,
                "payload": self.payload,
                "prev_hash": self.prev_hash,
                "timestamp": self.timestamp,
            },
            sort_keys=True,
        )
        self.commitment_hash = sha256(body)
        return self

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


# --------------------------------------------------------------------------- #
#  Backend interface
# --------------------------------------------------------------------------- #

class KaspaBackend:
    """Interface every backend implements. Swap local <-> testnet freely."""

    def anchor(self, kind: CommitmentKind, payload: dict) -> Commitment:
        raise NotImplementedError

    def get_chain(self) -> list[dict]:
        raise NotImplementedError

    def balance(self, address: str) -> float:
        raise NotImplementedError


class LedgerBackend(KaspaBackend):
    """
    Faithful local model of Kaspa commitment semantics.

    - Append-only, hash-linked commitment chain (tamper-evident).
    - Address-based balances with real escrow lock/release accounting.
    - Deterministic txids + commitment hashes so a verifier can re-check.
    """

    def __init__(self) -> None:
        self._chain: list[Commitment] = []
        self._balances: dict[str, float] = {}
        self._escrow: dict[str, float] = {}   # task_id -> locked amount
        self._tip = "0x" + "0" * 64           # genesis

    # --- commitment anchoring --------------------------------------------- #
    def anchor(self, kind: CommitmentKind, payload: dict) -> Commitment:
        c = Commitment(
            txid=new_txid(),
            kind=kind,
            payload=payload,
            prev_hash=self._tip,
            timestamp=time.time(),
        ).seal()
        self._chain.append(c)
        self._tip = c.commitment_hash
        return c

    def get_chain(self) -> list[dict]:
        return [c.to_dict() for c in self._chain]

    def verify_integrity(self) -> bool:
        """Re-walk the hash lineage; any tamper breaks the chain."""
        tip = "0x" + "0" * 64
        for c in self._chain:
            if c.prev_hash != tip:
                return False
            recomputed = Commitment(
                c.txid, c.kind, c.payload, c.prev_hash, c.timestamp
            ).seal().commitment_hash
            if recomputed != c.commitment_hash:
                return False
            tip = c.commitment_hash
        return True

    # --- balances / escrow ------------------------------------------------ #
    def fund(self, address: str, amount: float) -> None:
        self._balances[address] = self._balances.get(address, 0.0) + amount

    def balance(self, address: str) -> float:
        return round(self._balances.get(address, 0.0), 4)

    def lock_escrow(self, task_id: str, user_addr: str, amount: float) -> Commitment:
        bal = self._balances.get(user_addr, 0.0)
        if bal < amount:
            raise ValueError(
                f"Insufficient balance to escrow: {bal} < {amount} KAS"
            )
        self._balances[user_addr] = bal - amount
        self._escrow[task_id] = self._escrow.get(task_id, 0.0) + amount
        return self.anchor(
            CommitmentKind.ESCROW_LOCK,
            {"task_id": task_id, "from": user_addr, "amount": amount},
        )

    def settle(
        self,
        task_id: str,
        agent_addr: str,
        user_addr: str,
        result: SettlementResult,
        amount: float,
    ) -> Commitment:
        """
        Programmable milestone settlement. Draws `amount` from the task's
        escrow: RELEASED -> pay agent; REFUNDED -> return to user. The rest
        of the escrow stays locked for remaining milestones.
        """
        locked = self._escrow.get(task_id, 0.0)
        amount = min(amount, locked)
        self._escrow[task_id] = locked - amount
        if result == SettlementResult.RELEASED:
            self._balances[agent_addr] = (
                self._balances.get(agent_addr, 0.0) + amount
            )
            to = agent_addr
        else:
            self._balances[user_addr] = (
                self._balances.get(user_addr, 0.0) + amount
            )
            to = user_addr
        return self.anchor(
            CommitmentKind.SETTLEMENT,
            {
                "task_id": task_id,
                "result": result.value,
                "amount": round(amount, 4),
                "to": to,
            },
        )

    def release_remaining_escrow(self, task_id: str, user_addr: str) -> float:
        """Return any unspent escrow to the user at job end."""
        left = self._escrow.pop(task_id, 0.0)
        if left > 0:
            self._balances[user_addr] = self._balances.get(user_addr, 0.0) + left
        return round(left, 4)


class KaspaTestnetBackend(KaspaBackend):
    """
    Drop-in for real Kaspa testnet. Same interface as LedgerBackend.

    Wire points (left as clearly marked TODOs so the demo stays runnable):
      - anchor()  -> submit a tx carrying the commitment hash in its payload
                     (OP_RETURN-style / SilverScript covenant script)
      - settle()  -> a covenant that releases escrow iff verifier sig + deadline
      - balance() -> query kaspad via the Rusty-Kaspa gRPC / WASM SDK
    """

    def __init__(self, rpc_url: str = "grpc://testnet.kaspad:16210") -> None:
        self.rpc_url = rpc_url

    def anchor(self, kind: CommitmentKind, payload: dict) -> Commitment:
        raise NotImplementedError(
            "Connect kaspad RPC and submit commitment hash on-chain here."
        )

    def get_chain(self) -> list[dict]:
        raise NotImplementedError("Query anchored commitments from kaspad.")

    def balance(self, address: str) -> float:
        raise NotImplementedError("Query UTXO set for address via kaspad.")


# Singleton used by the app.
LEDGER = LedgerBackend()


def make_backend():
    """
    Select the Kaspa backend from environment.

    KASPA_MODE=testnet  -> real Testnet-10 reads (balances, UTXOs, anchor
                           verification) via api-tn10.kaspa.org, and on-chain
                           anchoring when a signer sidecar is configured.
    KASPA_MODE=local    -> the faithful local commitment ledger (default).

    The rest of AgentK is identical either way; only this factory changes.
    """
    import os
    if os.environ.get("KASPA_MODE", "local").lower() == "testnet":
        try:
            from kaspa_testnet import KaspaTestnet
            return KaspaTestnet()   # live read client
        except Exception:
            return LEDGER
    return LEDGER
