"""
new_wallet.py — generate a fresh Testnet-10 wallet for AgentK (pure Python).

Run:  python new_wallet.py

Prints an ADDRESS (paste into the faucet) and a PRIVATE KEY (put in .env).
Throwaway testnet key — never reuse for mainnet or anything of value.
"""
from kaspa import Keypair, NetworkType

kp = Keypair.random()
addr = kp.to_address(NetworkType.Testnet)

print("\n===============  AgentK testnet wallet  ===============\n")
print("ADDRESS  (give this to the faucet):")
print("   " + str(addr) + "\n")
print("PRIVATE KEY  (put in .env, keep secret):")
print("   " + str(kp.private_key) + "\n")
print("Next steps:")
print("  1. Fund the address:  https://faucet-tn10.kaspanet.io")
print("  2. In .env:  KASPA_PRIVATE_KEY=" + str(kp.private_key))
print("  3. Run AgentK — settlements sign & broadcast automatically.")
print("\n=======================================================\n")
