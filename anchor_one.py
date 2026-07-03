"""
anchor_one.py — pre-flight check: send ONE real autonomous transaction.

Run BEFORE your demo:  python anchor_one.py

Uses the treasury key in .env to sign + broadcast a tiny self-payment on
Kaspa Testnet-10 carrying a test commitment hash, then prints the explorer
link. If this works, the full autonomous demo will work.
"""
import asyncio, time, json
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass
from kaspa_layer import sha256
from kaspa_autonomous import SIGNER

async def main():
    print("AgentK autonomous pre-flight — Kaspa Testnet-10\n")
    if not SIGNER.enabled:
        print("Signer disabled:", SIGNER.error)
        print("Fix .env (KASPA_PRIVATE_KEY) and retry."); return 1
    print("Treasury:", SIGNER.address)
    bal = await SIGNER.balance()
    print("Balance :", bal, "tKAS")
    if bal <= 0.3:
        print("\nFund the treasury first: https://faucet-tn10.kaspanet.io"); return 1
    h = sha256(json.dumps({"agentk":"preflight","ts":int(time.time())}))
    print("Anchoring test commitment", h[:18]+"…")
    r = await SIGNER.send(SIGNER.address, 0.2, payload_hex=h[2:])
    if r.ok:
        print("\nSUCCESS — real on-chain transaction:")
        print("  " + r.explorer_url)
        print("\nAutonomous settlement is live. Run the demo.")
        return 0
    print("\nFAILED:", r.error); return 1

raise SystemExit(asyncio.run(main()))
