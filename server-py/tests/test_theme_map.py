import unittest

from app.dashboard_projection import compact_dashboard, overview_dashboard
from app.theme_map import build_important_changes, build_theme_map


NOW = 2_000_000_000_000


def asset(chain, token, *, symbol="SAME", change=4, volume=2000,
          volume_at=NOW - 1000, change_at=NOW - 1000):
    return {
        "chainId": chain, "token": token, "kind": "candidate",
        "symbol": symbol, "name": symbol + " asset", "price": 1.25,
        "priceCurrency": "USD", "volume24h": volume, "change24h": change,
        "fieldTimes": {"price": NOW - 1000, "volume24h": volume_at,
                       "change24h": change_at},
        "fieldScopes": {"price": "token", "volume24h": "token", "change24h": "token"},
        "fieldSources": {"price": "OKX", "volume24h": "OKX", "change24h": "OKX"},
        "fieldObservations": {"price": {"currency": "USD"},
                              "volume24h": {"currency": "USD"}},
    }


def relation(chain, token, ticker, *, pool="0xpool", liquidity=2000, level="A"):
    return {
        "id": f"{chain}:{pool}:{ticker}:{token}", "chainId": chain,
        "token": token, "pool": pool, "stock": "0xstock", "status": "verified",
        "level": level, "ticker": "SPOOFED", "liquidityUsd": liquidity,
        "liquidityAt": NOW - 1000, "checkedAt": NOW - 1000,
        "stockIdentity": {"ticker": ticker, "nameEn": ticker + " xStock",
                          "sourceUrl": "https://issuer.example/assets"},
    }


class ThemeMapTest(unittest.TestCase):
    def test_contract_identity_official_ticker_and_breadth_are_distinct(self):
        first = "0x" + "1" * 40
        second = "0x" + "2" * 40
        rows = [asset("56", first, change=3), asset("196", second, change=-2),
                asset("196", first, change=100)]
        evidence = [
            relation("56", first, "NVDA", pool="0xa", liquidity=2200),
            relation("56", first, "NVDA", pool="0xb", liquidity=1100),
            relation("196", second, "NVDA", pool="0xc"),
            relation("56", first, "AMD", pool="0xd", liquidity=1200),
            relation("196", first, "FALSE", pool="0xe", level="B"),
        ]
        result = build_theme_map(rows, evidence, NOW)
        self.assertEqual(result["totalAssets"], 2)
        self.assertEqual({bubble["key"] for bubble in result["bubbles"]},
                         {f"56:{first}", f"196:{second}"})
        themes = {row["ticker"]: row for row in result["themes"]}
        self.assertEqual(themes["NVDA"]["breadth"],
                         {"rising": 1, "falling": 1, "flat": 0, "unknown": 0,
                          "valid": 2, "total": 2, "window": "24h"})
        self.assertEqual(themes["AMD"]["breadth"]["total"], 1)
        self.assertNotIn("SPOOFED", themes)
        self.assertNotIn("FALSE", themes)
        primary = next(row for row in result["bubbles"] if row["token"] == first)
        self.assertEqual(primary["primaryTicker"], "NVDA")
        self.assertEqual(primary["otherTickers"], ["AMD"])
        self.assertEqual(primary["relation"]["pool"], "0xa")
        self.assertEqual(primary["volume24h"]["status"], "current")

    def test_each_metric_uses_its_own_time_and_known_usd_scope(self):
        token = "0x" + "1" * 40
        stale = asset("56", token, change=9, volume_at=NOW - 900_001)
        stale["fieldTimes"].pop("change24h")
        result = build_theme_map([stale], [relation("56", token, "NVDA")], NOW)
        bubble = result["bubbles"][0]
        self.assertEqual(bubble["volume24h"]["status"], "stale")
        self.assertIsNone(bubble["volume24h"]["value"])
        self.assertEqual(bubble["change24h"]["status"], "unknown-time")
        self.assertIsNone(bubble["change24h"]["value"])
        self.assertEqual(result["themes"][0]["breadth"]["unknown"], 1)
        unsupported = asset("56", token, volume=5000)
        unsupported["fieldObservations"]["volume24h"]["currency"] = "BNB"
        bubble = build_theme_map([unsupported], [relation("56", token, "NVDA")], NOW)["bubbles"][0]
        self.assertEqual(bubble["volume24h"]["status"], "unsupported-currency")
        self.assertIsNone(bubble["volume24h"]["value"])
        unknown_scope = asset("56", token)
        unknown_scope["fieldScopes"]["change24h"] = "pool:0xpool"
        bubble = build_theme_map([unknown_scope], [relation("56", token, "NVDA")], NOW)["bubbles"][0]
        self.assertEqual(bubble["change24h"]["status"], "unsupported-scope")

    def test_change_feed_separates_pool_creation_from_site_verification(self):
        token = "0x" + "1" * 40
        current = relation("56", token, "NVDA")
        current.update(poolCreatedAt=NOW - 100_000_000, discoveredAt=NOW - 50_000,
                       firstSeen=NOW - 60_000)
        signals = [
            {"id": "verified-1", "kind": "verified", "chainId": "56",
             "asset": token, "pool": "0xpool", "t": NOW - 10_000},
            {"id": "verified-old", "kind": "verified", "chainId": "56",
             "asset": token, "pool": "0xpool", "t": NOW - 100_000_000},
            {"id": "other-pool", "kind": "verified", "chainId": "56",
             "asset": token, "pool": "0xunverified", "t": NOW - 2000},
        ]
        result = build_important_changes(signals, [current], [asset("56", token)], NOW)
        self.assertEqual(len(result["items"]), 1)
        event = result["items"][0]
        self.assertEqual(event["type"], "relation-verified")
        self.assertEqual(event["ticker"], "NVDA")
        self.assertEqual(event["occurredAt"], NOW - 100_000_000)
        self.assertEqual(event["discoveredAt"], NOW - 50_000)
        self.assertEqual(event["verifiedAt"], NOW - 10_000)
        self.assertEqual(event["relation"]["pool"], "0xpool")

    def test_projection_metadata_changes_only_when_evidence_or_freshness_changes(self):
        token = "0x" + "1" * 40
        row = asset("56", token)
        proof = relation("56", token, "NVDA")
        first = build_theme_map([row], [proof], NOW)
        seconds_later = build_theme_map([row], [proof], NOW + 30_000)
        self.assertEqual(first, seconds_later)
        expired = build_theme_map([row], [proof], NOW + 901_000)
        self.assertEqual(expired["bubbles"][0]["change24h"]["status"], "stale")
        self.assertEqual(expired["themes"][0]["breadth"]["unknown"], 1)

    def test_projection_keeps_new_metadata_in_full_and_overview(self):
        token = "0x" + "1" * 40
        theme_map = build_theme_map([asset("56", token)], [relation("56", token, "NVDA")], NOW)
        changes = build_important_changes([], [], [], NOW)
        original = {"now": NOW, "unified": {"assets": [], "stockTokens": [],
                    "relations": [], "signals": [], "themeMap": theme_map,
                    "importantChanges": changes}}
        full = compact_dashboard(original)
        overview = overview_dashboard(full)
        self.assertEqual(overview["unified"]["themeMap"], theme_map)
        self.assertEqual(full["unified"]["importantChanges"], changes)


if __name__ == "__main__":
    unittest.main()
