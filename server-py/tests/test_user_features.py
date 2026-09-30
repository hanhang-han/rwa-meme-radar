import asyncio
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from app import developer_access as access, user_features
from app.api import developer
from app import main

TOKEN = '56:0x' + 'a' * 40
OTHER = '196:0x' + 'b' * 40


class UserFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DEVELOPER_DB_PATH': self.tmp.name + '/access.sqlite',
            'DEVELOPER_COOKIE_SECURE': '0', 'DEVELOPER_SHARED_INVITE_CODE': 'test-code',
            'RADAR_OPERATOR_EMAILS': '', 'RADAR_OPERATOR_USER_IDS': '',
            'TELEGRAM_BOT_TOKEN': '', 'TELEGRAM_BOT_USERNAME': ''})
        self.env.start()
        with patch.object(access, '_password_hash', return_value=b'unit-test-hash'):
            self.first = access.register('first@example.com', 'test-code', 'long test password 123', 'peer1')
            self.second = access.register('second@example.com', 'test-code', 'long test password 123', 'peer2')
        self.app = FastAPI()
        self.app.include_router(developer.router, prefix='/api')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://testserver')
        self.health = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://testserver')

    async def asyncTearDown(self):
        await self.client.aclose(); await self.health.aclose()
        self.env.stop(); self.tmp.cleanup()

    def signed_in(self, account=None, client=None):
        user, cookie, csrf = account or self.first
        (client or self.client).cookies.set(developer.COOKIE_NAME, cookie)
        return {'X-CSRF-Token': csrf, 'Origin': 'https://cliperx.com'}

    async def test_watches_and_preferences_require_login_csrf_and_same_origin(self):
        for path in ('watches', 'alerts'):
            self.assertEqual((await self.client.get('/api/developer/' + path)).status_code, 401)
        self.assertEqual((await self.client.patch('/api/developer/watches', json={'add': [TOKEN]})).status_code, 401)
        headers = self.signed_in()
        for supplied in ({}, {'X-CSRF-Token': 'wrong'}, {**headers, 'Origin': 'https://attacker.example'}):
            response = await self.client.patch('/api/developer/watches', json={'add': [TOKEN]}, headers=supplied)
            self.assertEqual(response.status_code, 403)
        result = await self.client.patch('/api/developer/watches', json={'add': [TOKEN]}, headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['userId'], self.first[0]['id'])
        self.assertEqual(result.headers['Cache-Control'], 'no-store')
        denied = await self.client.put('/api/developer/alerts', json={'largeTrade': True})
        self.assertEqual(denied.status_code, 403)

    async def test_account_isolation_and_server_only_user_identity(self):
        headers = self.signed_in()
        response = await self.client.patch('/api/developer/watches', json={
            'add': [TOKEN], 'user_id': self.second[0]['id']}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.signed_in(self.second)
        self.assertEqual((await self.client.get('/api/developer/watches')).json()['items'], [])
        self.assertEqual(user_features.list_watches(self.first[0]['id']), [TOKEN])

    async def test_invalid_keys_and_duplicate_normalization(self):
        headers = self.signed_in()
        for key in ('stock:', 'stock:NVDA;DROP TABLE users', 'x:0x' + 'a' * 40,
                    '56:0x123', '0:0x' + 'a' * 40, 'stock:<script>'):
            result = await self.client.patch('/api/developer/watches', json={'add': [key]}, headers=headers)
            self.assertEqual(result.status_code, 400, key)
        result = await self.client.patch('/api/developer/watches', json={
            'add': [TOKEN, TOKEN.upper(), '056:0x' + 'a' * 40, 'stock:nvda']}, headers=headers)
        self.assertEqual(set(result.json()['items']), {TOKEN, 'stock:NVDA'})
        conflict = await self.client.patch('/api/developer/watches', json={
            'add': [TOKEN], 'remove': [TOKEN.upper()]}, headers=headers)
        self.assertEqual(conflict.status_code, 400)

    async def test_idempotent_incremental_writes_preserve_other_device_items(self):
        user = self.first[0]['id']
        user_features.change_watches(user, [TOKEN], [])
        with ThreadPoolExecutor(max_workers=2) as executor:
            tasks = [executor.submit(user_features.change_watches, user, ['stock:NVDA'], []),
                     executor.submit(user_features.change_watches, user, [OTHER], [TOKEN])]
            for task in tasks:
                task.result()
        self.assertEqual(set(user_features.change_watches(user, [OTHER], [TOKEN])), {'stock:NVDA', OTHER})
        self.assertEqual(set(user_features.change_watches(user, ['stock:NVDA'], [])), {'stock:NVDA', OTHER})

    async def test_limit_atomicity_and_simultaneous_last_slot(self):
        user = self.first[0]['id']
        original = ['stock:T' + str(i) for i in range(199)]
        user_features.change_watches(user, original, [])
        def add(key):
            try:
                user_features.change_watches(user, [key], [])
                return 200
            except access.AccessError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(add, ['stock:NVDA', 'stock:TSLA']))
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(len(user_features.list_watches(user)), 200)
        before = set(user_features.list_watches(user))
        with self.assertRaises(access.AccessError):
            user_features.change_watches(user, ['stock:X', 'stock:Y'], [original[0]])
        self.assertEqual(set(user_features.list_watches(user)), before)

    async def test_alert_ranges_scope_and_no_delivery_claim(self):
        headers = self.signed_in()
        for threshold in (0, 99, 1_000_000_001, 'NaN', 'Infinity'):
            result = await self.client.put('/api/developer/alerts', headers=headers, json={'tradeUsd': threshold})
            self.assertEqual(result.status_code, 422)
        result = await self.client.put('/api/developer/alerts', headers=headers, json={
            'newPool': False, 'largeTrade': True, 'riskChange': True, 'tradeUsd': 500,
            'delivery': {'available': True}, 'sent': True})
        self.assertEqual(result.status_code, 200)
        saved = result.json()
        self.assertFalse(saved['delivery']['available'])
        self.assertFalse(saved['delivery']['linked'])
        self.assertEqual(saved['delivery']['reason'], 'telegram-not-configured')
        self.assertFalse(saved['delivery']['capabilities']['largeTrade']['available'])
        self.assertNotIn('delivered', saved['delivery'])
        self.assertNotIn('sent', saved)
        self.assertEqual(saved['tradeUsd'], 500)
        self.signed_in(self.second)
        other = (await self.client.get('/api/developer/alerts')).json()
        self.assertEqual(other['tradeUsd'], 10000)
        self.assertFalse(other['largeTrade'])
        self.assertEqual(user_features.alert_preferences(self.first[0]['id'])['tradeUsd'], 500)

    async def test_alert_delivery_readiness_is_from_real_bot_binding_not_preferences(self):
        from app import telegram_alerts
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': '123456789:' + 'x' * 32,
                                     'TELEGRAM_BOT_USERNAME': 'CliperxTestBot'}):
            available = user_features.alert_preferences(self.first[0]['id'])['delivery']
            self.assertTrue(available['available'])
            self.assertFalse(available['linked'])
            code = telegram_alerts.create_bind_code(self.first[0]['id'])['startUrl'].split('start=')[1]
            self.assertTrue(telegram_alerts.consume_update({'update_id': 1, 'message': {
                'chat': {'id': 123456, 'type': 'private'},
                'from': {'id': 123456, 'is_bot': False},
                'date': telegram_alerts.now_ms() // 1000, 'text': '/start ' + code}}))
            linked = user_features.alert_preferences(self.first[0]['id'])['delivery']
            self.assertTrue(linked['available'])
            self.assertTrue(linked['linked'])
            self.assertFalse(user_features.alert_preferences(self.second[0]['id'])['delivery']['linked'])
            self.assertFalse(set(linked) & {'sent', 'delivered', 'chat_id', 'token'})
            telegram_alerts.unlink(self.first[0]['id'])
            self.assertFalse(user_features.alert_preferences(self.first[0]['id'])['delivery']['linked'])

    async def test_public_health_and_operator_gate_before_diagnostics(self):
        payload = AsyncMock(return_value={'ok': True, 'privateMarker': 'internal'})
        with patch.object(main, 'health_data_payload', payload):
            result = await self.health.get('/api/health/data')
            self.assertEqual(result.status_code, 401)
            payload.assert_not_awaited()
            self.signed_in(client=self.health)
            self.assertEqual((await self.health.get('/api/health/data')).status_code, 403)
            payload.assert_not_awaited()
            # A self-declared mailbox is insufficient to acquire operations access.
            with patch.dict(os.environ, {'RADAR_OPERATOR_EMAILS': self.first[0]['email']}):
                self.assertEqual((await self.health.get('/api/health/data')).status_code, 403)
            with patch.dict(os.environ, {'RADAR_OPERATOR_USER_IDS': self.first[0]['id']}):
                allowed = await self.health.get('/api/health/data')
                self.assertEqual(allowed.status_code, 200)
                self.assertEqual(allowed.headers['Cache-Control'], 'no-store')
                self.assertTrue(allowed.json()['ok'])
            payload.assert_awaited_once()
        self.health.cookies.clear()
        self.assertEqual((await self.health.get('/api/health/live')).status_code, 200)
        fake_store = type('Store', (), {'fetchone': AsyncMock(return_value=(1,))})()
        with patch('app.db.store', AsyncMock(return_value=fake_store)), patch.object(access, 'ready', return_value=True):
            self.assertEqual((await self.health.get('/api/health/ready')).status_code, 200)

    async def test_operator_id_cannot_be_replaced_by_matching_email_or_wrong_additional_email(self):
        with patch.dict(os.environ, {'RADAR_OPERATOR_USER_IDS': self.first[0]['id'],
                                     'RADAR_OPERATOR_EMAILS': self.first[0]['email'].upper()}):
            self.assertTrue(user_features.is_operator(self.first[0]))
            self.assertFalse(user_features.is_operator({**self.first[0], 'id': self.second[0]['id']}))
            self.assertFalse(user_features.is_operator({**self.first[0], 'email': self.second[0]['email']}))
            response = developer._response(self.first[0], 'test-cookie', 'test-csrf')
            self.assertIn(b'"operator":true', response.body)


if __name__ == '__main__':
    unittest.main()
