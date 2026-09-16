"""In-memory snapshot assembled from the research store, mirroring the Node
xLayerState/dashboardState shapes so the frontend contract stays identical."""
import time
from typing import Any

from .db import store

BASE_QUOTE_SYMBOLS = {
    "USDT", "USDC", "USDG", "DAI", "WOKB", "OKB", "WETH", "ETH", "WBTC", "BTC", "XBTC",
    "WBNB", "BTCB", "WTBC", "USD1", "XUSD",
}
CHAIN_NAMES = {"196": "X Layer", "56": "BNB Smart Chain", "4663": "Robinhood Chain"}


def fresh(at, age_ms=900_000) -> bool:
    return bool(at) and time.time() * 1000 - at < age_ms


class DashboardData:
    """Reads the persisted facts and renders the unified payload."""

    def __init__(self):
        self.assets: list[dict] = []
        self.relations: list[dict] = []
        self.stock_tokens: list[dict] = []
        self.updated_at: float | None = None

    async def reload(self, chain: str = "196") -> None:
        s = await store(chain)
        assets = await s.all("asset")
        relations = await s.all("relation")
        stocks = await s.all("stock")
        scans = {r["token"]: r for r in await s.all("scan")}

        candidate_ids = {a["token"] for a in assets if a.get("kind") == "candidate"}
        relations = [r for r in relations if r.get("token") in candidate_ids]

        for a in assets:
            a["chainId"] = chain
            a["chain"] = chain
            a["chainName"] = CHAIN_NAMES.get(chain, chain)
            a.setdefault("fieldTimes", {})

        for r in relations:
            r["chainId"] = chain
            r.setdefault("liquidityUsd", None)

        for st in stocks:
            st["chainId"] = chain
            st["chain"] = chain
            st.setdefault("tokenContractAddress", st.get("tokenContractAddress"))

        self.assets = assets
        self.relations = relations
        self.stock_tokens = stocks
        self.updated_at = max(
            [a.get("updatedAt") or 0 for a in assets] + [r.get("checkedAt") or 0 for r in relations] + [0]
        ) or None

    # -- unified payload pieces -------------------------------------------------

    def visible_assets(self) -> list[dict]:
        out = []
        for a in self.assets:
            if a.get("kind") != "candidate":
                continue
            if str(a.get("symbol", "")).upper() in BASE_QUOTE_SYMBOLS:
                continue
            out.append(a)
        return out

    def verified_relations(self, max_age_ms=3_600_000) -> list[dict]:
        now = time.time() * 1000
        return [r for r in self.relations if r.get("status") == "verified" and now - r.get("checkedAt", 0) < max_age_ms]

    def groups(self) -> list[dict]:
        by_symbol: dict[str, list[dict]] = {}
        for a in self.visible_assets():
            by_symbol.setdefault(str(a.get("symbol", "?")).upper(), []).append(a)
        groups = []
        for symbol, members in by_symbol.items():
            members.sort(key=lambda m: -(m.get("volume24h") or 0))
            groups.append({
                "symbol": symbol,
                "members": [f"{m['chainId']}:{m['token']}" for m in members],
                "volume24h": sum(m.get("volume24h") or 0 for m in members if m.get("volume24h") is not None) or None,
            })
        groups.sort(key=lambda g: -(g["volume24h"] or 0))
        return groups

    def metrics(self) -> dict:
        verified = self.verified_relations()
        valued = [r for r in verified if r.get("liquidityUsd") is not None and fresh(r.get("liquidityAt") or r.get("checkedAt"))]
        now = time.time() * 1000
        return {
            "verifiedPools": len(verified),
            "verifiedAssets": len({r["token"] for r in verified}),
            "actionableAssets": len({
                r["token"] for r in verified
                if (r.get("liquidityUsd") or 0) >= 1000 and fresh(r.get("liquidityAt") or r.get("checkedAt"))
            }),
            "pairedLiquidityUsd": sum(r["liquidityUsd"] for r in valued) if valued else None,
            "liquidityCoverage": {
                "valued": len(valued),
                "total": len(verified),
            },
            "newRelations24h": sum(1 for r in self.relations if (r.get("firstSeen") or 0) >= now - 86_400_000),
        }

    def payload(self) -> dict:
        return {
            "now": time.time() * 1000,
            "unified": {
                "version": 2,
                "sources": [],
                "assets": self.visible_assets(),
                "stockTokens": self.stock_tokens,
                "relations": self.relations,
                "signals": [],
                "groups": self.groups(),
                "quality": {},
                "metrics": self.metrics(),
                "sectors": [],
                "distribution": [],
                "capabilities": [],
                "collection": {"updatedAt": self.updated_at},
            },
        }


DATA = DashboardData()
_loaded_at = 0.0


async def reload_data() -> None:
    global _loaded_at
    await DATA.reload("196")
    _loaded_at = time.time()


async def reload_if_stale(max_age: float = 10.0) -> None:
    if time.time() - _loaded_at > max_age:
        await reload_data()
