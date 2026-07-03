"""
kaspa_autonomous.py
-------------------
FULLY AUTONOMOUS on-chain settlement for AgentK — pure Python, using the
OFFICIAL Rusty-Kaspa Python SDK (pypi.org/project/kaspa, by the Kaspa core
team). No Node.js, no browser clicks, no sidecar.

The user writes a goal. Agents plan, negotiate, work, verify — and settle
with REAL transactions on Kaspa Testnet-10, signed automatically by this
module with the key in .env.

What it does per job:
  - Derives the treasury address from KASPA_PRIVATE_KEY (must be funded —
    use https://faucet-tn10.kaspanet.io).
  - On each RELEASED settlement: sends a real payment to the agent's real
    testnet address, carrying that task's commitment hash as the tx payload.
  - On job completion: anchors the job's master hash on-chain.
  - Every txid is emitted to the UI with a live explorer link.

Verified offline in development (official SDK):
  keypair generation, testnet address derivation, transaction construction
  with payload, signing, fee calculation. Broadcast requires live network
  (RpcClient via the Kaspa Public Node Network resolver).
"""

from __future__ import annotations
import os
import asyncio
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from kaspa import (
        PrivateKey, Keypair, NetworkType, PaymentOutput, Resolver, RpcClient,
        create_transactions, kaspa_to_sompi, sompi_to_kaspa,
    )
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False

NETWORK_ID = "testnet-10"
# Explorer link template, configurable because testnet explorer sites come and
# go. {txid} is substituted. The REST API link below always works and returns
# the raw tx JSON including the anchored payload — the strongest proof.
EXPLORER_TX = os.environ.get(
    "EXPLORER_TX_URL",
    "https://tn10.kaspa.stream/transactions/{txid}",
)
API_TX = "https://api-tn10.kaspa.org/transactions/{txid}"


@dataclass
class OnChainResult:
    ok: bool
    txid: str = ""
    explorer_url: str = ""
    api_url: str = ""
    error: str = ""


class AutonomousSigner:
    """
    Owns the treasury key and signs settlement transactions automatically.

    Lifecycle per payment:
      connect (resolver) -> fetch UTXOs -> create_transactions(payload=...)
      -> sign -> submit -> disconnect. All official-SDK calls.
    """

    def __init__(self) -> None:
        self.enabled = False
        self.address = ""
        self.error = ""
        if not SDK_AVAILABLE:
            self.error = "kaspa SDK not installed (pip install kaspa)"
            return
        key_hex = os.environ.get("KASPA_PRIVATE_KEY", "").strip()
        if not key_hex:
            self.error = "KASPA_PRIVATE_KEY not set in .env"
            return
        try:
            self._priv = PrivateKey(key_hex)
            self.address = str(
                self._priv.to_keypair().to_address(NetworkType.Testnet)
            )
            self.enabled = True
        except Exception as e:
            self.error = f"Invalid KASPA_PRIVATE_KEY: {e}"

    # ------------------------------------------------------------------ #

    async def send(self, to_address: str, amount_kas: float,
                   payload_hex: str = "", label: str = "") -> OnChainResult:
        """
        Build, sign, and broadcast one real transaction on Testnet-10.
        `payload_hex` (e.g. a commitment hash) is embedded in the tx payload
        so the agreement is permanently, publicly verifiable.
        Hard timeout so a network problem can never hang the job.
        """
        if not self.enabled:
            return OnChainResult(False, error=self.error)
        timeout = float(os.environ.get("ONCHAIN_TIMEOUT_S", "45") or 45)
        try:
            res = await asyncio.wait_for(
                self._send_inner(to_address, amount_kas, payload_hex),
                timeout=timeout,
            )
            if res.ok:
                import time as _t
                ONCHAIN_LOG.append({
                    "ts": _t.time(), "label": label or "transaction",
                    "txid": res.txid, "explorer_url": res.explorer_url,
                    "api_url": res.api_url, "amount_kas": amount_kas,
                    "to": to_address, "payload_hash": "0x" + payload_hex[:16],
                })
            return res
        except asyncio.TimeoutError:
            return OnChainResult(
                False,
                error=f"Network timeout after {timeout:.0f}s — check internet "
                      "access to the Kaspa Public Node Network",
            )

    async def _send_inner(self, to_address: str, amount_kas: float,
                          payload_hex: str) -> OnChainResult:
        client = RpcClient(resolver=Resolver(), network_id=NETWORK_ID)
        try:
            await client.connect()
            utxos = await client.get_utxos_by_addresses(
                {"addresses": [self.address]}
            )
            entries = utxos.get("entries", []) if isinstance(utxos, dict) \
                else getattr(utxos, "entries", [])
            if not entries:
                return OnChainResult(
                    False,
                    error=f"No UTXOs — fund {self.address} at the faucet",
                )
            res = create_transactions(
                entries=entries,
                change_address=self.address,
                network_id=NETWORK_ID,
                outputs=[PaymentOutput(
                    to_address, kaspa_to_sompi(float(amount_kas))
                )],
                payload=bytes.fromhex(payload_hex) if payload_hex else None,
                priority_fee=kaspa_to_sompi(0.0001),
            )
            txid = ""
            for tx in res["transactions"]:
                tx.sign([self._priv])
                txid = await tx.submit(client)
            return OnChainResult(
                True, str(txid),
                EXPLORER_TX.format(txid=txid),
                api_url=API_TX.format(txid=txid),
            )
        except Exception as e:
            return OnChainResult(False, error=str(e)[:200])
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def balance(self) -> float:
        if not self.enabled:
            return 0.0
        client = RpcClient(resolver=Resolver(), network_id=NETWORK_ID)
        try:
            await client.connect()
            b = await client.get_balance_by_address({"address": self.address})
            bal = b.get("balance", 0) if isinstance(b, dict) \
                else getattr(b, "balance", 0)
            return float(sompi_to_kaspa(int(bal)))
        except Exception:
            return 0.0
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass


def real_agent_address() -> str:
    """A real, freshly generated Testnet-10 address for an agent identity."""
    if SDK_AVAILABLE:
        return str(Keypair.random().to_address(NetworkType.Testnet))
    # SDK missing: fall back to demo-shaped address
    import secrets
    return "kaspatest:" + secrets.token_hex(20)


ONCHAIN_LOG: list[dict] = []   # every real tx this server has broadcast


SIGNER = AutonomousSigner()