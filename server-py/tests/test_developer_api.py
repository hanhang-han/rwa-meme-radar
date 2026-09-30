import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from app import developer_access as access
from app.api import developer, public_v1


class DeveloperApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("DEVELOPER_DB_PATH")
        self.old_secure = os.environ.get("DEVELOPER_COOKIE_SECURE")
        self.old_shared_code = os.environ.get("DEVELOPER_SHARED_INVITE_CODE")
        self.old_shared_file = os.environ.get("DEVELOPER_SHARED_INVITE_FILE")
        self.shared_code = "test-shared-trial-code"
        os.environ["DEVELOPER_DB_PATH"] = self.directory.name + "/access.sqlite"
        os.environ["DEVELOPER_COOKIE_SECURE"] = "0"
        os.environ["DEVELOPER_SHARED_INVITE_CODE"] = self.shared_code
        os.environ["DEVELOPER_SHARED_INVITE_FILE"] = self.directory.name + "/missing-code.txt"
        self.app = FastAPI()
        self.app.include_router(developer.router, prefix="/api")
        self.app.include_router(public_v1.router, prefix="/api")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()
        if self.old_db is None:
            os.environ.pop("DEVELOPER_DB_PATH", None)
        else:
            os.environ["DEVELOPER_DB_PATH"] = self.old_db
        if self.old_secure is None:
            os.environ.pop("DEVELOPER_COOKIE_SECURE", None)
        else:
            os.environ["DEVELOPER_COOKIE_SECURE"] = self.old_secure
        if self.old_shared_code is None:
            os.environ.pop("DEVELOPER_SHARED_INVITE_CODE", None)
        else:
            os.environ["DEVELOPER_SHARED_INVITE_CODE"] = self.old_shared_code
        if self.old_shared_file is None:
            os.environ.pop("DEVELOPER_SHARED_INVITE_FILE", None)
        else:
            os.environ["DEVELOPER_SHARED_INVITE_FILE"] = self.old_shared_file
        self.directory.cleanup()

    async def _registered(self):
        result = await self.client.post("/api/developer/register", json={
            "email": "invited@example.com", "inviteCode": self.shared_code,
            "password": "a strong test password 123"})
        self.assertEqual(result.status_code, 200, result.text)
        return self.shared_code, result.json()["csrfToken"]

    async def test_shared_code_reused_for_different_emails_and_legacy_code_rejected(self):
        access.init()
        old_code = "cx_inv_" + "a" * 43
        with access.connection() as db:
            db.execute("INSERT INTO invitations VALUES (?,?,?,?,?,NULL,NULL)",
                       ("old", "old@example.com", access._hash(old_code),
                        int(time.time()), int(time.time()) + 86400))
        rejected = await self.client.post("/api/developer/register", json={
            "email": "old@example.com", "inviteCode": old_code,
            "password": "a strong test password 123"})
        self.assertEqual(rejected.status_code, 400)
        for email in ("one@example.com", "two@example.com"):
            registered = await self.client.post("/api/developer/register", json={
                "email": email, "inviteCode": self.shared_code,
                "password": "a strong test password 123"})
            self.assertEqual(registered.status_code, 200, registered.text)
        duplicate = await self.client.post("/api/developer/register", json={
            "email": "one@example.com", "inviteCode": self.shared_code,
            "password": "a strong test password 123"})
        self.assertEqual(duplicate.status_code, 409)
        with access.connection() as db:
            row = db.execute("SELECT consumed_at,code_hash FROM invitations WHERE id='old'").fetchone()
        self.assertIsNone(row["consumed_at"])
        self.assertNotEqual(row["code_hash"], self.shared_code)

    async def test_registration_fails_closed_without_code_and_uses_protected_file(self):
        with patch.dict(os.environ, {"DEVELOPER_SHARED_INVITE_CODE": ""}):
            unavailable = await self.client.post("/api/developer/register", json={
                "email": "file@example.com", "inviteCode": self.shared_code,
                "password": "a strong test password 123"})
            self.assertEqual(unavailable.status_code, 503)
            path = Path(os.environ["DEVELOPER_SHARED_INVITE_FILE"])
            path.write_text(self.shared_code + "\n", encoding="utf-8")
            os.chmod(path, 0o644)
            world_readable = await self.client.post("/api/developer/register", json={
                "email": "file@example.com", "inviteCode": self.shared_code,
                "password": "a strong test password 123"})
            self.assertEqual(world_readable.status_code, 503)
            os.chmod(path, 0o600)
            registered = await self.client.post("/api/developer/register", json={
                "email": "file@example.com", "inviteCode": self.shared_code,
                "password": "a strong test password 123"})
            self.assertEqual(registered.status_code, 200, registered.text)

    async def test_successful_registration_limits_are_atomic_per_ip_and_global(self):
        with patch.object(access, "_password_hash", return_value=b"x" * 32), \
                patch.object(access, "MAX_NEW_ACCOUNTS_PER_IP_DAY", 2):
            for index in range(2):
                access.register(f"peer{index}@example.com", self.shared_code,
                                "a strong test password 123", "203.0.113.1")
            with self.assertRaises(access.AccessError) as peer_limited:
                access.register("peer2@example.com", self.shared_code,
                                "a strong test password 123", "203.0.113.1")
            self.assertEqual(peer_limited.exception.status, 429)
            # A different peer can still register; the failed insert did not
            # consume a global successful-signup slot.
            access.register("other@example.com", self.shared_code,
                            "a strong test password 123", "203.0.113.2")
        with patch.object(access, "_password_hash", return_value=b"x" * 32), \
                patch.object(access, "MAX_NEW_ACCOUNTS_GLOBAL_DAY", 3):
            with self.assertRaises(access.AccessError) as global_limited:
                access.register("global@example.com", self.shared_code,
                                "a strong test password 123", "203.0.113.3")
            self.assertEqual(global_limited.exception.status, 429)

    async def test_cold_invalid_key_is_401_and_wrong_code_skips_scrypt(self):
        key = "cx_trial_" + "a" * 16 + "." + "b" * 43
        denied = await self.client.get("/api/v1/snapshot/latest", headers={"Authorization": "Bearer " + key})
        self.assertEqual(denied.status_code, 401)
        with patch.object(access, "_password_hash", side_effect=AssertionError("hash must not run")):
            rejected = await self.client.post("/api/developer/register", json={
                "email": "guess@example.com", "inviteCode": "incorrect-code",
                "password": "a strong test password 123"})
        self.assertEqual(rejected.status_code, 400)

    async def test_login_ip_limit_cannot_be_bypassed_by_rotating_email(self):
        with patch.object(access, "_password_hash", return_value=b"x" * 32):
            for number in range(30):
                denied = await self.client.post("/api/developer/login", json={
                    "email": f"absent{number}@example.com", "password": "any password"})
                self.assertEqual(denied.status_code, 401)
            limited = await self.client.post("/api/developer/login", json={
                "email": "another@example.com", "password": "any password"})
        self.assertEqual(limited.status_code, 429)
        self.assertIn("Retry-After", limited.headers)

    async def test_old_auth_attempts_are_globally_pruned(self):
        access.init()
        with patch.object(access, "_now", return_value=1_000_000):
            for number in range(40):
                access._admission(((f"login-ip-email:{number}", 8, 900),))
        with access.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM auth_attempts").fetchone()[0], 40)
        with patch.object(access, "_now", return_value=1_000_000 + 2 * 86400):
            access._admission((("login-ip:new", 8, 900),))
        with access.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM auth_attempts").fetchone()[0], 1)

    async def test_password_hash_concurrency_is_bounded(self):
        def fake_register(*_):
            time.sleep(0.15)
            return {"id": "x", "email": "a@example.com"}, "token", "csrf"
        with patch.object(access, "register", side_effect=fake_register):
            results = await asyncio.gather(*(self.client.post("/api/developer/register", json={
                "email": "a@example.com", "inviteCode": self.shared_code,
                "password": "a strong test password 123"}) for _ in range(3)))
        self.assertEqual(sorted(r.status_code for r in results), [200, 200, 429])

    async def test_session_csrf_key_one_time_and_revoke(self):
        _, csrf = await self._registered()
        me = await self.client.get("/api/developer/me")
        self.assertEqual(me.json()["csrfToken"], csrf)
        self.assertIn("HttpOnly", self.client.cookies.jar._cookies["testserver.local"]["/"]["cx_trial_session"]._rest)
        missing_csrf = await self.client.post("/api/developer/keys", json={"name": "trial"})
        self.assertEqual(missing_csrf.status_code, 403)
        made = await self.client.post("/api/developer/keys", json={"name": "trial"},
                                      headers={"X-CSRF-Token": csrf})
        self.assertEqual(made.status_code, 200, made.text)
        secret, key_id = made.json()["secret"], made.json()["key"]["id"]
        self.assertTrue(secret.startswith("cx_trial_"))
        listed = await self.client.get("/api/developer/keys")
        self.assertNotIn(secret, listed.text)
        self.assertNotIn(secret, (await self.client.get("/api/developer/me")).text)
        with patch.object(public_v1, "_index", AsyncMock(return_value={
            "revision": 2, "asOf": 123, "publishedAt": 124,
            "assetCount": 1, "verifiedRelationCount": 0, "nameClueCount": 0, "byTicker": {}})):
            got = await self.client.get("/api/v1/snapshot/latest", headers={"Authorization": "Bearer " + secret})
            self.assertEqual(got.status_code, 200, got.text)
            self.assertNotIn(secret, got.text)
            revoked = await self.client.delete("/api/developer/keys/" + key_id,
                                               headers={"X-CSRF-Token": csrf})
            self.assertEqual(revoked.status_code, 200)
            denied = await self.client.get("/api/v1/snapshot/latest", headers={"Authorization": "Bearer " + secret})
            self.assertEqual(denied.status_code, 401)

    async def test_org_quota_applies_across_keys(self):
        _, csrf = await self._registered()
        first = (await self.client.post("/api/developer/keys", json={"name": "one"},
                                        headers={"X-CSRF-Token": csrf})).json()["secret"]
        second = (await self.client.post("/api/developer/keys", json={"name": "two"},
                                         headers={"X-CSRF-Token": csrf})).json()["secret"]
        with patch.object(public_v1, "_index", AsyncMock(return_value={
            "revision": 2, "asOf": 123, "publishedAt": 124,
            "assetCount": 1, "verifiedRelationCount": 0, "nameClueCount": 0, "byTicker": {}})):
            for number in range(access.PER_MINUTE):
                key = first if number % 2 else second
                result = await self.client.get("/api/v1/snapshot/latest", headers={"Authorization": "Bearer " + key})
                self.assertEqual(result.status_code, 200, result.text)
            limited = await self.client.get("/api/v1/snapshot/latest", headers={"Authorization": "Bearer " + first})
            self.assertEqual(limited.status_code, 429)
            self.assertIn("Retry-After", limited.headers)
        usage = (await self.client.get("/api/developer/usage")).json()
        self.assertEqual(usage["used"], access.PER_MINUTE)

    async def test_public_relation_index_separates_verified_and_name_only(self):
        now = 1_000_000
        payload = {"now": now, "unified": {
            "relations": [{"chainId": "56", "token": "0x" + "a" * 40,
                           "stock": "0x" + "b" * 40, "pool": "0x" + "c" * 40,
                           "ticker": "NVDA", "status": "verified", "level": "A",
                           "evidenceStatus": "qualified", "validUntil": now + 10_000,
                           "checkedAt": now, "ruleVersion": "test"}],
            "assets": [{"kind": "candidate", "chainId": "56", "token": "0x" + "a" * 40,
                        "match": {"ticker": "NVDA", "matchType": "name", "keyword": "Nvidia"}},
                       {"kind": "candidate", "chainId": "196", "token": "0x" + "d" * 40,
                        "match": {"ticker": "NVDA", "matchType": "name", "keyword": "Nvidia"}}]}}
        with patch.object(public_v1.time, "time", return_value=now / 1000):
            indexed = public_v1._build_index(payload, 7, now)
        rows = indexed["byTicker"]["NVDA"]
        self.assertEqual([r["grade"] for r in rows], ["A", "B"])
        self.assertIsNone(rows[1]["poolAddress"])

    async def test_risk_check_unknown_is_not_clear(self):
        checks = public_v1._public_checks({"checks": {
            "thin_spike": {"status": "triggered", "reason": "threshold-exceeded",
                           "evidence": {"change24hPct": 1200, "threshold": 1000,
                                        "volume24hUsd": 123456}}}})
        self.assertEqual(set(checks), {'wash_suspect', 'thin_spike', 'contract_risk',
                                       'concentrated', 'holder_anomaly', 'liquidity_unlock'})
        self.assertEqual(checks['liquidity_unlock']['status'], 'unknown')
        self.assertEqual(checks["wash_suspect"]["status"], "unknown")
        self.assertEqual(checks["thin_spike"]["status"], "triggered")
        self.assertNotIn("volume24hUsd", checks["thin_spike"]["evidence"])

    async def test_numeric_stock_code_is_valid_filter(self):
        self.assertTrue(public_v1.TICKER.fullmatch("9992"))
        self.assertTrue(public_v1.TICKER.fullmatch("1024"))

    async def test_risk_uses_cached_side_source_without_projection_revision(self):
        _, csrf = await self._registered()
        secret = (await self.client.post("/api/developer/keys", json={"name": "risk"},
                                         headers={"X-CSRF-Token": csrf})).json()["secret"]
        address = "0x" + "d" * 40
        now = int(time.time() * 1000)
        fact = {"token": address, "chainId": "196", "kind": "candidate",
                "updatedAt": now - 1000, "fieldTimes": {}}
        snapshot = {"assets": {"196:" + address: {"coingecko": {
            "provider": "CoinGecko", "liquidity": 10_000, "volume24h": 600_000,
            "fieldTimes": {"liquidity": now, "volume24h": now},
            "fieldScopes": {"liquidity": "token-aggregate", "volume24h": "token-aggregate"},
        }}}}
        fake_store = type("Store", (), {"get": AsyncMock(return_value=fact)})()
        with patch.object(public_v1, "store", AsyncMock(return_value=fake_store)), \
                patch("app.market_quotes._read_snapshot", return_value=snapshot), \
                patch.object(public_v1, "_index", AsyncMock(side_effect=AssertionError("must not parse projection"))):
            result = await self.client.get(f"/api/v1/token/196/{address}/risk",
                                           headers={"Authorization": "Bearer " + secret})
        self.assertEqual(result.status_code, 200, result.text)
        body = result.json()
        self.assertNotIn("revision", body)
        self.assertIn("calculatedAt", body)
        self.assertEqual(body["checks"]["wash_suspect"]["status"], "triggered")
        self.assertEqual(body["checks"]["wash_suspect"]["evidence"]["volumeLiquidityRatio"], 60)


if __name__ == "__main__":
    unittest.main()
