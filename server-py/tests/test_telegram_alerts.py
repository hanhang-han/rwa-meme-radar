import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse, parse_qs

import httpx
from fastapi import FastAPI

from app import developer_access as access, telegram_alerts as alerts, user_features
from app.api import developer, telegram

NOW = 1_800_000_000_000
TOKEN = '0x'+'a'*40
OTHER = '0x'+'b'*40
POOL = '0x'+'c'*40
KEY = '56:'+TOKEN
BOT_ENV = {'TELEGRAM_BOT_TOKEN': '123456:'+('a'*35), 'TELEGRAM_BOT_USERNAME': 'CliperXTestBot'}


def update(code, ident=1, chat=12345, now=NOW, kind='private', sender=None):
    return {'update_id': ident, 'message': {'date': now//1000, 'text': '/start '+code,
        'chat': {'id': chat, 'type': kind}, 'from': {'id': chat if sender is None else sender, 'is_bot': False}}}


def asset(flags=()):
    return {'chainId': '56', 'token': TOKEN, 'symbol': 'TEST', 'riskFlags': list(flags),
            'riskAssessment': {'checks': {flag: {'status': 'triggered'} for flag in flags}}}


def payload(flags=()):
    return {'unified': {'assets': [asset(flags)], 'relations': [{'chainId': '56', 'token': TOKEN,
        'ticker': 'NVDA', 'level': 'A', 'status': 'verified'}]}, 'realtime': {'cursor': 1000}}


def row(seq, event, body, at=NOW+1000):
    return {'id': seq, 'event': event, 'body': json.dumps(body), 'at': at}


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {**BOT_ENV, 'DEVELOPER_DB_PATH': self.directory.name+'/access.sqlite',
            'DEVELOPER_SHARED_INVITE_CODE': 'test-invite', 'DEVELOPER_COOKIE_SECURE': '0'})
        self.env.start()
        self.clock = NOW
        self.clock_patch = patch.object(alerts, 'now_ms', side_effect=lambda: self.clock)
        self.clock_patch.start()
        with patch.object(access, '_password_hash', return_value=b'test-password-hash'):
            self.first, self.cookie, self.csrf = access.register('first@example.com', 'test-invite', 'long secure password 123', 'one')
            self.second, _, _ = access.register('second@example.com', 'test-invite', 'long secure password 123', 'two')
        self.user = self.first['id']
        alerts.init()
        # Watch timestamps in this fixture use the controlled millisecond clock.
        with patch.object(user_features.time, 'time', return_value=(NOW-1000)/1000):
            user_features.change_watches(self.user, [KEY], [])
        self.app = FastAPI()
        self.app.include_router(developer.router, prefix='/api')
        self.app.include_router(telegram.router, prefix='/api')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://testserver',
                                       cookies={developer.COOKIE_NAME: self.cookie})

    async def asyncTearDown(self):
        await self.client.aclose()
        self.clock_patch.stop()
        self.env.stop()
        self.directory.cleanup()
        alerts._verified_bot = None

    def bind(self, user=None, ident=1, chat=12345):
        link = alerts.create_bind_code(user or self.user)
        code = parse_qs(urlparse(link['startUrl']).query)['start'][0]
        self.assertTrue(alerts.consume_update(update(code, ident, chat, now=self.clock)))
        return code

    def event(self, ident='test', category='riskChange', key=KEY, at=None):
        return {'category': category, 'eventKey': ident, 'assetKey': key, 'at': at or self.clock,
                'flags': ['contract_risk'], 'symbol': 'TEST', 'ticker': 'NVDA', 'pool': POOL, 'usd': 20000}

    def enqueue(self, event):
        with access.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            count = alerts._enqueue(db, event, self.clock)
            db.commit()
        return count

    def queue(self):
        with access.connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM telegram_outbox ORDER BY created_at,id')]

    async def test_disabled_configuration_makes_no_network_and_bind_is_503(self):
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': ''}), \
             patch.object(httpx, 'AsyncClient', side_effect=AssertionError('unexpected network')):
            self.assertFalse(alerts.status(self.user)['available'])
            self.assertEqual(await alerts.tick(), {'accepted': 0, 'skipped': 1})
            with self.assertRaises(access.AccessError) as failure:
                alerts.create_bind_code(self.user)
            self.assertEqual(failure.exception.status, 503)
        self.assertFalse(alerts.capabilities()['largeTrade']['available'])

    async def test_http_auth_csrf_private_one_use_link_and_unlink(self):
        anonymous = await self.client.get('/api/developer/telegram', headers={'cookie': ''})
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual((await self.client.post('/api/developer/telegram/bind')).status_code, 403)
        reply = await self.client.post('/api/developer/telegram/bind', headers={'x-csrf-token': self.csrf})
        self.assertEqual(reply.status_code, 200)
        code = parse_qs(urlparse(reply.json()['startUrl']).query)['start'][0]
        self.assertEqual(reply.json()['expiresAt'], NOW+600000)
        with access.connection() as db:
            raw = db.execute('SELECT code_hash FROM telegram_bind_codes').fetchone()[0]
        self.assertNotEqual(raw, code)
        self.assertNotIn(code, raw)
        self.assertFalse(alerts.consume_update(update(code, 1, kind='group')))
        self.assertFalse(alerts.consume_update(update(code, 2, sender=99999)))
        self.assertTrue(alerts.consume_update(update(code, 3)))
        self.assertFalse(alerts.consume_update(update(code, 4, chat=99999)))
        status = (await self.client.get('/api/developer/telegram')).json()
        self.assertTrue(status['linked'])
        self.assertNotIn('chat_id', status)
        self.assertEqual((await self.client.delete('/api/developer/telegram')).status_code, 403)
        response = await self.client.delete('/api/developer/telegram', headers={'x-csrf-token': self.csrf})
        self.assertFalse(response.json()['linked'])

    async def test_expiration_rotation_and_chat_cannot_bind_two_active_accounts(self):
        one = alerts.create_bind_code(self.user)
        two = alerts.create_bind_code(self.user)
        first = parse_qs(urlparse(one['startUrl']).query)['start'][0]
        second = parse_qs(urlparse(two['startUrl']).query)['start'][0]
        self.assertFalse(alerts.consume_update(update(first, 1)))
        self.assertTrue(alerts.consume_update(update(second, 2)))
        other = alerts.create_bind_code(self.second['id'])
        other_code = parse_qs(urlparse(other['startUrl']).query)['start'][0]
        self.assertFalse(alerts.consume_update(update(other_code, 3)))
        self.clock += 600001
        self.assertFalse(alerts.consume_update(update(other_code, 4, chat=99999, now=self.clock)))
        self.assertFalse(alerts.status(self.second['id'])['linked'])

    async def test_no_historical_alert_and_risk_requires_new_trigger_after_baseline(self):
        self.bind()
        self.assertEqual(alerts.process_events([], payload(), 10, 1), 0)
        self.clock += 1000
        risk = row(11, 'projection.delta', {'upserts': {'assets': [asset(['contract_risk'])]}}, self.clock)
        self.assertEqual(alerts.process_events([risk], payload(['contract_risk']), 11, 1), 1)
        self.assertEqual(alerts.process_events([risk], payload(['contract_risk']), 11, 1), 0)
        self.clock += 1000
        same = row(12, 'projection.delta', {'upserts': {'assets': [asset(['contract_risk'])]}}, self.clock)
        self.assertEqual(alerts.process_events([same], payload(['contract_risk']), 12, 1), 0)
        self.assertEqual(len(self.queue()), 1)
        self.assertIn('合约风险', json.loads(self.queue()[0]['payload'])['text'])
        self.assertNotIn('first@example.com', self.queue()[0]['payload'])

    async def test_only_confirmed_recent_factory_pool_and_stock_watch_match(self):
        self.bind()
        user_features.change_watches(self.user, ['stock:NVDA'], [KEY])
        with access.connection() as db:
            db.execute('UPDATE user_watches SET created_at=?', (NOW-1000,))
        alerts.process_events([], payload(), 10, 1)
        self.clock += 1000
        relation = {'chainId': '56', 'token': TOKEN, 'ticker': 'NVDA', 'pool': POOL,
                    'id': 'pair', 'status': 'verified',
                    'poolCreatedAt': self.clock, 'confirmationStatus': 'provisional', 'factoryEventId': 'factory-event',
                    'stockIdentity': {'verificationStatus': 'official'}}
        self.assertEqual(alerts.process_events([row(11, 'relationship', {'relation': relation})], payload(), 11, 1), 0)
        relation['confirmationStatus'] = 'confirmed'
        current = payload()
        current['unified']['relations'] = [relation]
        self.assertEqual(alerts.process_events([row(12, 'relationship', {'relation': relation})], current, 12, 1), 1)
        self.assertEqual(alerts.process_events([row(13, 'relationship', {'relation': relation})], current, 13, 1), 0)
        self.assertEqual(self.queue()[0]['category'], 'newPool')
        self.assertIn('NVDA', self.queue()[0]['payload'])

    async def test_preferences_cooldown_unwatch_and_unlink_cancel_pending(self):
        self.bind()
        self.clock += 1000
        self.assertEqual(self.enqueue(self.event('one')), 1)
        self.assertEqual(self.enqueue(self.event('duplicate-but-new-id')), 0)
        self.assertEqual(self.enqueue(self.event('provisional', 'largeTrade')), 0)
        user_features.change_watches(self.user, [], [KEY])
        self.assertIsNone(alerts._claim())
        self.assertEqual(self.queue()[0]['status'], 'cancelled')
        with patch.object(user_features.time, 'time', return_value=self.clock/1000):
            user_features.change_watches(self.user, [KEY], [])
        self.clock += 1
        self.assertEqual(self.enqueue(self.event('new')), 1)
        alerts.unlink(self.user)
        self.assertTrue(all(item['status'] == 'cancelled' for item in self.queue()))
        self.assertIsNone(alerts._claim())

    async def test_daily_limit_is_per_user_utc_and_retries_reserve_once(self):
        self.bind()
        for index in range(21):
            self.clock = NOW+1000+index*900001
            key = '56:0x'+f'{index:040x}'
            with patch.object(user_features.time, 'time', return_value=(self.clock-1)/1000):
                user_features.change_watches(self.user, [key], [])
            self.assertEqual(self.enqueue(self.event(str(index), key=key)), 1)
            row = alerts._claim()
            if index < 20:
                self.assertIsNotNone(row)
                alerts._finish(row, result={'message_id': index})
            else:
                self.assertIsNone(row)
        self.assertEqual(sum(row['status'] == 'sent' for row in self.queue()), 20)
        self.assertEqual(self.queue()[-1]['error_code'], 'daily-limit')

    async def test_delivery_429_retry_bound_and_ambiguous_timeout_never_retries(self):
        self.bind()
        self.clock += 1000
        self.enqueue(self.event('rate-limit'))
        client = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(429, json={'ok': False, 'error_code': 429, 'parameters': {'retry_after': 7}})))
        for attempt in range(3):
            outcome = await alerts.dispatch(client)
            self.assertEqual(outcome['failed'], 1)
            self.assertEqual(self.queue()[0]['attempts'], attempt+1)
            self.clock += 7000
        self.assertEqual(self.queue()[0]['status'], 'failed')
        self.assertIsNone(alerts._claim())
        self.clock += 900001
        self.enqueue(self.event('timeout'))
        client.post = AsyncMock(side_effect=httpx.ReadTimeout('secret URL must not escape'))
        await alerts.dispatch(client)
        self.assertEqual(self.queue()[-1]['status'], 'uncertain')
        self.assertEqual(self.queue()[-1]['error_code'], 'delivery-unknown')
        self.assertIsNone(alerts._claim())

    async def test_success_targets_only_bound_private_chat_no_paid_broadcast(self):
        self.bind(chat=87654)
        self.clock += 1000
        self.enqueue(self.event())
        client = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(200, json={'ok': True, 'result': {'message_id': 77}})))
        self.assertEqual((await alerts.dispatch(client))['accepted'], 1)
        parameters = client.post.call_args.kwargs['json']
        self.assertEqual(parameters['chat_id'], '87654')
        self.assertIs(parameters['allow_paid_broadcast'], False)
        self.assertNotIn('parse_mode', parameters)
        self.assertIn('https://cliperx.com/dashboard/#/asset/56/'+TOKEN, parameters['text'])
        self.assertEqual(self.queue()[0]['status'], 'sent')
        await alerts.dispatch(client)
        self.assertEqual(client.post.call_count, 1)

    async def test_interrupted_send_is_terminal_and_stop_disables_recipient(self):
        code = self.bind()
        self.clock += 1000
        self.enqueue(self.event())
        self.assertIsNotNone(alerts._claim())
        self.clock += 60001
        self.assertIsNone(alerts._claim())
        self.assertEqual(self.queue()[0]['status'], 'uncertain')
        command = update(code, 2, now=self.clock)
        command['message']['text'] = '/stop'
        self.assertTrue(alerts.consume_update(command))
        self.assertFalse(alerts.status(self.user)['linked'])

    async def test_missing_confirmed_trade_path_cannot_enqueue_large_swap(self):
        self.bind()
        self.clock += 1000
        user_features.save_alert_preferences(self.user, {'newPool': True, 'riskChange': True, 'largeTrade': True, 'tradeUsd': 100})
        self.assertEqual(self.enqueue(self.event('trade', 'largeTrade')), 0)
        self.assertEqual(self.queue(), [])

    async def test_bot_switch_does_not_inherit_old_update_offset(self):
        self.bind(ident=1000)
        with patch.dict(os.environ, {'TELEGRAM_BOT_USERNAME': 'OtherTestBot'}):
            self.assertFalse(alerts.status(self.user)['linked'])
            self.bind(ident=1)
            self.assertTrue(alerts.status(self.user)['linked'])

    async def test_reorg_after_queue_cancels_pool_alert_before_any_send(self):
        self.bind()
        self.clock += 1000
        event = {**self.event('pool', 'newPool'), 'relationId': 'pair', 'factoryEventId': 'factory-event'}
        self.enqueue(event)
        relation = {'id': 'pair', 'chainId': '56', 'token': TOKEN, 'pool': POOL,
            'status': 'verified', 'factoryEventId': 'factory-event', 'confirmationStatus': 'confirmed',
            'stockIdentity': {'verificationStatus': 'official'}}
        # Relation invalidation may lag the factory's authoritative reorg marker.
        research = SimpleNamespace(get=AsyncMock(return_value=relation), fetchone=AsyncMock(return_value=('orphaned',)))
        client = SimpleNamespace(post=AsyncMock())
        with patch('app.db.store', new=AsyncMock(return_value=research)):
            self.assertEqual((await alerts.dispatch(client))['accepted'], 0)
        client.post.assert_not_awaited()
        self.assertEqual(self.queue()[0]['error_code'], 'pool-no-longer-confirmed')

    async def test_reorg_before_intake_does_not_use_historical_confirmed_relation(self):
        self.bind()
        alerts.process_events([], payload(), 10, 1)
        self.clock += 1000
        relation = {'id': 'pair', 'chainId': '56', 'token': TOKEN, 'pool': POOL,
            'status': 'verified', 'factoryEventId': 'factory-event', 'confirmationStatus': 'confirmed',
            'poolCreatedAt': self.clock, 'stockIdentity': {'verificationStatus': 'official'}}
        current = payload()
        current['unified']['relations'] = [{**relation, 'status': 'invalid', 'confirmationStatus': 'orphaned'}]
        self.assertEqual(alerts.process_events([row(11, 'relationship', {'relation': relation})], current, 11, 1), 0)
        self.assertEqual(self.queue(), [])

    async def test_unlink_during_source_lookup_prevents_pending_network_send(self):
        self.bind()
        self.clock += 1000
        self.enqueue(self.event('pool', 'newPool'))
        async def lookup(_):
            alerts.unlink(self.user)
            return True
        client = SimpleNamespace(post=AsyncMock())
        with patch.object(alerts, '_current_pool', new=lookup):
            await alerts.dispatch(client)
        client.post.assert_not_awaited()
        self.assertEqual(self.queue()[0]['status'], 'cancelled')

    async def test_delayed_delivery_still_enforces_fifteen_minutes_between_notifications(self):
        self.bind()
        self.clock += 1000
        self.enqueue(self.event('first'))
        attempt = alerts._claim()
        alerts._finish(attempt, failure=alerts.BotFailure('rate-limited', retry_after=960, retryable=True))
        self.clock += 960000
        self.enqueue(self.event('second'))
        attempt = alerts._claim()
        self.assertEqual(attempt['event_key'], 'first')
        alerts._finish(attempt, result={'message_id': 1})
        self.assertIsNone(alerts._claim())
        self.clock += 900000
        attempt = alerts._claim()
        self.assertEqual(attempt['event_key'], 'second')

    async def test_scan_waits_for_projection_publication_before_consuming_journal(self):
        self.bind()
        alerts.process_events([], payload(), 10, 1)
        self.clock += 1000
        change = row(11, 'projection.delta', {'upserts': {'assets': [asset(['concentrated'])]}}, self.clock)
        research = SimpleNamespace(fetchone=AsyncMock(return_value=(1, 11)), fetchall=AsyncMock(return_value=[change]))
        current = payload(['concentrated'])
        current['realtime']['cursor'] = 10
        with patch('app.db.store', new=AsyncMock(return_value=research)), \
             patch('app.realtime_projection.read_projection_json', new=AsyncMock(side_effect=lambda _: json.dumps(current))):
            self.assertEqual(await alerts.scan_events(), 0)
            research.fetchall.assert_not_awaited()
            current['realtime']['cursor'] = 11
            self.assertEqual(await alerts.scan_events(), 1)
        self.assertIn('持仓集中', self.queue()[0]['payload'])

    async def test_rejected_server_retry_and_error_text_never_contains_response_details(self):
        client = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(503, json={'ok': False, 'error_code': 503})))
        with self.assertRaises(alerts.BotFailure) as failure:
            await alerts._api(client, 'sendMessage', {})
        self.assertTrue(failure.exception.retryable)
        client.post.return_value = httpx.Response(403, json={'ok': False, 'error_code': 'secret-token'})
        with self.assertRaises(alerts.BotFailure) as failure:
            await alerts._api(client, 'sendMessage', {})
        self.assertEqual(str(failure.exception), 'bot-rejected-403')

    async def test_bot_username_mismatch_prevents_polling_and_sending(self):
        alerts.create_bind_code(self.user)
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = httpx.Response(200, json={'ok': True, 'result': {'is_bot': True, 'username': 'WrongBot'}})
        with patch.object(httpx, 'AsyncClient', return_value=client):
            self.assertEqual((await alerts.tick())['failed'], 1)
        self.assertEqual(client.post.await_count, 1)
        self.assertTrue(client.post.call_args.args[0].endswith('/getMe'))
        self.assertEqual(alerts.status(self.user)['worker']['reason'], 'bot-username-mismatch')


if __name__ == '__main__':
    unittest.main()
