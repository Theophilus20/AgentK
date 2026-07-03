# AgentK — Programmable Multi-Agent Coordination on Kaspa

> Autonomous AI agents that negotiate, commit, verify work, and settle
> trustlessly — using Kaspa as a coordination, commitment, and settlement
> layer, not just a payment rail.

A user submits a goal and a budget. A **Coordinator** AI decomposes it into a
DAG of tasks. Specialist agents **bid**, an agreement is **anchored on Kaspa**,
each agent **executes**, an independent **Verifier** checks the work, and funds
**settle programmatically** — released on verified delivery, refunded otherwise.
Every step is a hash-linked commitment on the Kaspa layer.

## Why this isn't "just payments"

Kaspa is used as five distinct layers:

| Layer | What it does |
|-------|--------------|
| **Commitment** | Task agreements are anchored + hash-sealed; no agent can later deny terms |
| **Escrow** | Budget is locked before any work begins |
| **Milestone** | Funds release only against a verified proof-of-work hash |
| **Settlement** | Programmable `IF verified AND on-time THEN release ELSE refund` — the natural home for **Covenants / SilverScript** |
| **Reputation** | Agent performance is anchored as on-chain commitments the Coordinator reads when selecting agents |

The workflow is a **DAG**, which mirrors Kaspa's own BlockDAG — parallel tasks
map directly onto parallel structure.

## Architecture

```
USER ─▶ Coordinator (decompose → DAG)
          │
          ├─ escrow lock ───────────────▶ Kaspa commitment layer
          │
    per task:
      match agent → negotiate bid → anchor TASK commitment
        → agent executes → anchor WORK_PROOF (output hash)
        → Verifier agent checks → programmable SETTLEMENT
        → anchor REPUTATION update
          │
          ▼
   verified result to user  +  refund of unspent escrow
```

## Files

- `kaspa_layer.py` — commitment chain, escrow, settlement, integrity check.
  Ships a faithful local `LedgerBackend`; `KaspaTestnetBackend` is the
  drop-in stub showing where kaspad / SilverScript calls go.
- `agents.py` — agent identities (wallet, pubkey, skill tags, reputation),
  marketplace registry, and the **OpenRouter `gpt-4o-mini`** client
  (with an offline mock so it runs with no key).
- `orchestrator.py` — the Coordinator runtime; streams the lifecycle.
- `server.py` — FastAPI + SSE.
- `static/index.html` — minimalist black-and-white dashboard.

## Run — fully autonomous

```bash
pip install -r requirements.txt        # includes the OFFICIAL Kaspa Python SDK
copy .env.example .env                 # (cp on Mac/Linux) then edit .env:
#   OPENROUTER_API_KEY = your OpenRouter key
#   KASPA_PRIVATE_KEY  = run `python new_wallet.py`, fund the printed
#                        address at https://faucet-tn10.kaspanet.io
python anchor_one.py                   # pre-flight: one real on-chain tx
uvicorn server:app --port 8099         # open http://localhost:8099
```

The user only writes the goal. Everything else is autonomous:
the Coordinator plans, specialists bid and work (real `gpt-4o-mini` calls,
no mock), the Verifier audits, and settlements are **signed and broadcast
automatically** on Kaspa Testnet-10 by the treasury key — a real payment to
each agent's real testnet address carrying the task's commitment hash as tx
payload, plus one master anchor per job. Every txid appears in the feed with
a live `explorer-tn10.kaspa.org` link.

Signing uses the **official Rusty-Kaspa Python SDK** (`pip install kaspa`,
published by the Kaspa core team) — no Node.js, no browser wallet needed.
Users are protected without holding keys: the Verifier gates every payment,
and a strict AI Dispute Arbiter handles refund requests (denying style
complaints and abuse, upholding objective failures with an on-chain ruling
and a reputation penalty). Jobs run server-side — close the browser, agents
keep working; reconnect and the feed catches up. Real
payments are scaled by `ONCHAIN_PAY_SCALE` (default 0.1) to preserve faucet
funds across demo runs; set it to 1.0 to pay full bids.

## Wiring real Kaspa testnet

**This is now implemented** (`kaspa_testnet.py`), targeting Testnet-10 via the
public REST API `https://api-tn10.kaspa.org`. Toccata (Kaspa's L1 covenant
hardfork, activated ~2026-06-30) makes on-chain escrow/pact rules real; AgentK
anchors each commitment hash in a transaction payload and can lock milestone
escrow into a covenant output.

**Read path — pure Python, works immediately:**
```bash
export KASPA_MODE=testnet
python kaspa_testnet.py kaspatest:your_address   # live balance + UTXOs
```
This reads live balances, UTXOs, confirms transactions, and — crucially —
`verify_anchor(txid, hash)` reads a commitment back off-chain so **anyone can
independently verify** an agreement was anchored. No trust required.

**Write path — needs a signer (Kaspa tx signing isn't available in Python):**
```bash
npm install kaspa express
export KASPA_PRIVATE_KEY=<hex key for a funded kaspatest: address>
node signer_sidecar.js        # runs on :7070, uses the official WASM SDK
```
`signer_sidecar.js` builds + signs the transaction with the **official
Rusty-Kaspa WASM SDK** (no hand-rolled crypto); Python broadcasts it via
`POST /transactions`. Get testnet KAS by running `kaspad --testnet
--netsuffix=10` and mining to your address, or via the Kaspa Discord
`#testnet` faucet channel.

> Note on why the write path is split: Kaspa transaction signing (key
> derivation, storage-mass, sig ops, covenant scripts) lives in the Rust/WASM
> SDK and has no pure-Python equivalent. Rather than fake a signer, AgentK
> calls the real one via a thin local sidecar. Everything else — balance
> reads, UTXO selection inputs, anchor verification, broadcast — is real
> Python talking to the live network.

## Guarantees demonstrated

- **Tamper-evidence** — `verify_integrity()` re-walks the hash lineage; any
  edit to a past commitment breaks the chain.
- **Value conservation** — escrow in = released to agents + refunded to user,
  checked end-to-end.
- **Independent verification** — the Verifier is a separate agent from the
  worker; low scores trigger refund, not payment.
