"""On-chain PairRegistry (X Layer 196) via web3.py: idempotent registration of
verified pairs plus the public read shape. Ports the Node registry.ts,
including the evidence-hash contract and the syncing lock with timeout."""
import asyncio
import json
import os
import time
from pathlib import Path

from web3 import Web3

CHAIN = 196
ARTIFACT = json.loads((Path(__file__).resolve().parents[2] / "contracts" / "artifacts" / "PairRegistry.json").read_text())
ABI = ARTIFACT["abi"]


def _rpc() -> str:
    return os.environ.get("XLAYER_RPC", "https://xlayerrpc.okx.com")


def _contract() -> str:
    return os.environ.get("REGISTRY_CONTRACT", "")


def _key() -> str:
    return os.environ.get("REGISTRY_OWNER_KEY", "")


def registry_configured() -> bool:
    return bool(Web3.is_address(_contract())) and bool(_key().startswith("0x") and len(_key()) == 66)


def evidence_json(r: dict) -> str:
    payload = {
        "block": r.get("block"),
        "chainId": str(r.get("chainId") or CHAIN),
        "checkedAt": r.get("checkedAt"),
        "liquidityUsd": r.get("liquidityUsd"),
        "meme": r["token"].lower(),
        "pool": r["pool"].lower(),
        "stock": r["stock"].lower(),
        "stockSide": r["stockSide"].lower(),
        "ticker": r.get("ticker"),
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def evidence_hash(r: dict) -> str:
    return Web3.keccak(text=evidence_json(r)).hex()


_w3: Web3 | None = None
_cache: tuple[float, list] | None = None


def w3() -> Web3:
    global _w3
    if _w3 is None:
        _w3 = Web3(Web3.HTTPProvider(_rpc(), request_kwargs={"timeout": 30}))
        if _w3.eth.chain_id != CHAIN:
            raise RuntimeError(f"wrong chain {_w3.eth.chain_id}, expect {CHAIN}")
    return _w3


async def onchain_pairs() -> list:
    global _cache
    contract = _contract()
    if not Web3.is_address(contract):
        return []
    if _cache and time.time() - _cache[0] < 60:
        return _cache[1]

    def read():
        c = w3().eth.contract(address=Web3.to_checksum_address(contract), abi=ABI)
        return [
            {
                "meme": p[0].lower(), "stock": p[1].lower(), "ticker": p[2],
                "evidence": p[3].hex(), "registeredAt": p[4],
            }
            for p in c.functions.all().call()
        ]

    rows = await asyncio.wait_for(asyncio.to_thread(read), 30)
    _cache = (time.time(), rows)
    return rows


async def registry_info(relations: list) -> dict:
    configured = registry_configured()
    try:
        rows = await onchain_pairs() if configured else []
    except Exception:
        rows = []
    done = {(p["meme"], p["stock"]) for p in rows}
    verified = [r for r in relations if r.get("status") == "verified"]
    by_pair = {(r["token"].lower(), r["stock"].lower()): r for r in verified}
    contract = _contract()
    return {
        "contract": contract or None,
        "chainId": CHAIN,
        "configured": configured,
        "explorer": f"https://www.okx.com/explorer/xlayer/address/{contract}" if contract else None,
        "onchainCount": len(rows),
        "pendingLocal": sum(1 for r in verified if (r["token"].lower(), r["stock"].lower()) not in done),
        "localVerified": len(verified),
        "pairs": [
            {**p, "evidenceSource": evidence_json(by_pair[(p["meme"], p["stock"])]) if (p["meme"], p["stock"]) in by_pair else None}
            for p in rows
        ],
    }


_syncing = False
_sync_started = 0.0


async def sync_registry(relations: list) -> None:
    global _syncing, _sync_started
    if not registry_configured():
        return
    if _syncing and time.time() - _sync_started < 900:
        return
    _syncing = True
    _sync_started = time.time()
    try:
        onchain = await onchain_pairs()
        done = {(p["meme"], p["stock"]) for p in onchain}
        pending = [r for r in relations if r.get("status") == "verified" and (r["token"].lower(), r["stock"].lower()) not in done]
        if not pending:
            return
        key = _key()
        account = w3().eth.account.from_key(key)
        if w3().eth.get_balance(account.address) == 0:
            print("[registry] owner wallet empty, skip sync")
            return
        contract = w3().eth.contract(address=Web3.to_checksum_address(_contract()), abi=ABI)

        def register_all():
            for r in pending:
                try:
                    tx = contract.functions.register(
                        Web3.to_checksum_address(r["token"].lower()),
                        Web3.to_checksum_address(r["stock"].lower()),
                        r.get("ticker", ""),
                        bytes.fromhex(evidence_hash(r)[2:]),
                    ).build_transaction({
                        "from": account.address,
                        "nonce": w3().eth.get_transaction_count(account.address),
                        "chainId": CHAIN,
                        "gas": 200_000,
                    })
                    tx["gasPrice"] = w3().eth.gas_price
                    signed = account.sign_transaction(tx)
                    h = w3().eth.send_raw_transaction(signed.raw_transaction)
                    w3().eth.wait_for_transaction_receipt(h, timeout=120)
                    print(f"[registry] registered {r.get('ticker')} {r['token'][:10]} tx={h.hex()}")
                except Exception as e:
                    print(f"[registry] register failed {r.get('ticker')}: {e}")

        await asyncio.wait_for(asyncio.to_thread(register_all), 60 * len(pending) + 60)
        global _cache
        _cache = None
    finally:
        _syncing = False
