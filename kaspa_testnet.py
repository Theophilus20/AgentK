"""
kaspa_testnet.py
----------------
REAL Kaspa integration for AgentK, targeting Testnet-10 (TN10).

Verified against the live public REST server (same codebase as api.kaspa.org):
    Testnet-10 base : https://api-tn10.kaspa.org
    Endpoints used  :
        GET  /info/network                      network + virtual DAA score
        GET  /addresses/{address}/balance       -> {"address","balance"} (sompi)
        GET  /addresses/{address}/utxos         -> [{outpoint, utxoEntry}, ...]
        GET  /transactions/{txId}               confirm an anchored tx
        POST /transactions                      submit a signed tx (broadcast)

Post-Toccata (mainnet activation ~2026-06-30 16:15 UTC; testable on TN10):
    - tx version 1 carries computeBudget, storageMass
    - output covenants + UTXO covenant_id enable on-chain escrow / pact rules
    We anchor AgentK commitments as a tx payload and (optionally) lock escrow
    into a covenant output whose spend path requires the verifier's condition.

Split of responsibilities (honest about what pure Python can/can't do):
    READ  path  -> 100% Python over REST. Fully implemented + runnable here:
                   balances, UTXO lookup, tx confirmation, chain reads.
    WRITE path  -> building + SIGNING a Kaspa tx needs the Rusty-Kaspa WASM
                   SDK (key mgmt, sig_op, storage-mass). Python can't sign a
                   Kaspa tx natively. So submit() posts an already-signed tx
                   built by a small Node signer sidecar (signer_sidecar.js),
                   OR broadcasts a pre-signed tx hex you pass in. The REST
                   POST /transactions call itself is implemented here.

1 KAS = 100_000_000 sompi.
"""

from __future__ import annotations
import httpx
from dataclasses import dataclass

SOMPI = 100_000_000

TN10_BASE = "https://api-tn10.kaspa.org"
MAINNET_BASE = "https://api.kaspa.org"


def kas(sompi: int) -> float:
    return round(sompi / SOMPI, 8)


def to_sompi(amount_kas: float) -> int:
    return int(round(amount_kas * SOMPI))


@dataclass
class Utxo:
    txid: str
    index: int
    amount_sompi: int
    script_public_key: str
    block_daa_score: int


class KaspaTestnet:
    """
    Real read/write client for Kaspa Testnet-10 via the public REST API.

    Reads work out of the box. Writes require a signer (sidecar or pre-signed
    hex) because Kaspa transaction signing is not available in pure Python.
    """

    def __init__(self, base: str = TN10_BASE, timeout: float = 20.0) -> None:
        self.base = base.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    # ------------------------------------------------------------------ #
    #  READ PATH  — fully implemented, pure Python
    # ------------------------------------------------------------------ #

    def network_info(self) -> dict:
        """Confirm the node is live and which network we're on."""
        r = self._client.get(f"{self.base}/info/network")
        r.raise_for_status()
        return r.json()

    def balance(self, address: str) -> float:
        """Live spendable balance for a kaspatest: address, in KAS."""
        r = self._client.get(f"{self.base}/addresses/{address}/balance")
        r.raise_for_status()
        return kas(int(r.json().get("balance", 0)))

    def utxos(self, address: str) -> list[Utxo]:
        """UTXO set for an address — the inputs a tx would spend."""
        r = self._client.get(f"{self.base}/addresses/{address}/utxos")
        r.raise_for_status()
        out: list[Utxo] = []
        for u in r.json():
            op = u.get("outpoint", {})
            e = u.get("utxoEntry", {})
            out.append(Utxo(
                txid=op.get("transactionId", ""),
                index=int(op.get("index", 0)),
                amount_sompi=int(e.get("amount", 0)),
                script_public_key=(e.get("scriptPublicKey", {}) or {})
                    .get("scriptPublicKey", ""),
                block_daa_score=int(e.get("blockDaaScore", 0)),
            ))
        return out

    def get_transaction(self, txid: str) -> dict:
        """Read an anchored tx back — this is how a verifier re-checks a proof."""
        r = self._client.get(f"{self.base}/transactions/{txid}",
                             params={"inputs": "true", "outputs": "true"})
        r.raise_for_status()
        return r.json()

    def is_confirmed(self, txid: str) -> bool:
        try:
            tx = self.get_transaction(txid)
        except httpx.HTTPStatusError:
            return False
        return bool(tx.get("is_accepted") or tx.get("accepting_block_hash"))

    def verify_anchor(self, txid: str, expected_payload_hex: str) -> bool:
        """
        Trustless check: does the on-chain tx carry the commitment we claim?
        AgentK anchors a commitment hash in the tx payload; anyone can verify
        it independently by reading the tx back from the network.
        """
        tx = self.get_transaction(txid)
        payload = (tx.get("payload") or "").lower()
        return expected_payload_hex.lower() in payload

    # ------------------------------------------------------------------ #
    #  WRITE PATH  — REST submit is implemented; signing is delegated
    # ------------------------------------------------------------------ #

    def submit_signed_transaction(self, signed_tx: dict) -> str:
        """
        Broadcast an already-signed transaction to the network.

        `signed_tx` must be the RPC-model JSON produced by the Rusty-Kaspa
        WASM SDK (or kaspa-cli). This is the real broadcast call; it returns
        the network-assigned transactionId on success.
        """
        r = self._client.post(
            f"{self.base}/transactions",
            json={"transaction": signed_tx},
        )
        r.raise_for_status()
        data = r.json()
        return data.get("transactionId") or data.get("txId", "")

    def anchor_commitment(self, signer, payload_hex: str,
                          amount_kas: float = 0.0,
                          to_address: str | None = None) -> str:
        """
        Anchor an AgentK commitment on-chain.

        The commitment hash goes into the tx payload so it is permanently,
        verifiably recorded. Optionally moves `amount_kas` (e.g. a milestone
        release) to `to_address`. The `signer` builds+signs the tx (see
        SignerSidecar below); we broadcast it and return the txid.
        """
        signed = signer.build_commitment_tx(
            payload_hex=payload_hex,
            amount_kas=amount_kas,
            to_address=to_address,
        )
        return self.submit_signed_transaction(signed)

    def close(self) -> None:
        self._client.close()


class SignerSidecar:
    """
    Thin bridge to the Node signer that owns the WASM SDK.

    Kaspa tx construction/signing (key derivation, storage-mass, sig ops,
    optional covenant scripts) lives in Rusty-Kaspa WASM. We run it as a tiny
    local HTTP sidecar (signer_sidecar.js) and call it here. This keeps the
    Python app clean while using the *official* signing code — no hand-rolled
    crypto.
    """

    def __init__(self, url: str = "http://127.0.0.1:7070") -> None:
        self.url = url.rstrip("/")
        self._client = httpx.Client(timeout=30.0)

    def build_commitment_tx(self, payload_hex: str, amount_kas: float,
                            to_address: str | None) -> dict:
        r = self._client.post(f"{self.url}/build", json={
            "payloadHex": payload_hex,
            "amountKas": amount_kas,
            "toAddress": to_address,
        })
        r.raise_for_status()
        return r.json()["signedTransaction"]


# --------------------------------------------------------------------------- #
#  quick self-test: read-only, hits the live TN10 network
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    client = KaspaTestnet()
    print("Network:", client.network_info())
    if len(sys.argv) > 1:
        addr = sys.argv[1]
        print(f"Balance {addr}: {client.balance(addr)} tKAS")
        us = client.utxos(addr)
        print(f"UTXOs: {len(us)} (first: {us[0] if us else 'none'})")
    client.close()
