"""The real API lifecycle owns and drains its nonblocking feed warmup."""
import asyncio
from unittest.mock import AsyncMock, Mock


def test_api_lifespan_drains_feed_jobs_before_closing_projection_readers(monkeypatch):
    from app import main, db, stream_hub, realtime_projection, activity_reads, live_market_store, demand_leases
    from app.api import misc, live_market

    async def check():
        await misc.stop_feed_reads()
        actual_projection_close = realtime_projection.stop_projection_queries
        await actual_projection_close()
        before = set(asyncio.all_tasks())
        gate = asyncio.Event()

        async def blocked_read(chain, **kwargs):
            await gate.wait()
            return {'trades': [], 'relationships': [], 'at': 123, 'scope': 'indexed-assets-dex-swaps'}

        async def projection_close():
            pending_feed = [t for t in asyncio.all_tasks()
                            if t not in before and not t.done() and t.get_name().startswith('feed-')]
            assert not pending_feed, 'feed warmup still owns work during shutdown'
            # Shielded projection work belongs to this next lifecycle stage;
            # drain the real producer before any storage connection is closed.
            await actual_projection_close()
            pending = [t for t in asyncio.all_tasks()
                       if t not in before and t is not asyncio.current_task() and not t.done()]
            assert not pending, 'projection producer still owns work before storage close'

        monkeypatch.setattr(main.developer_access, 'init', Mock())
        monkeypatch.setattr(db, 'store', AsyncMock())
        monkeypatch.setattr(db, 'close_all', AsyncMock())
        monkeypatch.setattr(stream_hub, 'start_hub', AsyncMock())
        monkeypatch.setattr(stream_hub, 'stop_hub', AsyncMock())
        monkeypatch.setattr(realtime_projection, 'stop_projection_queries', AsyncMock(side_effect=projection_close))
        monkeypatch.setattr(activity_reads, 'stop_activity_reads', AsyncMock())
        monkeypatch.setattr(live_market, 'stop_market_hubs', AsyncMock())
        monkeypatch.setattr(live_market_store, 'close_all', AsyncMock())
        monkeypatch.setattr(demand_leases, 'stop_lease_writer', AsyncMock())
        read = AsyncMock(side_effect=blocked_read)
        monkeypatch.setattr(misc, '_load_feed', read)
        monkeypatch.setattr(misc, 'read_projection_json', AsyncMock(return_value='{"assets":[],"signals":[]}'))
        context = main.lifespan(main.app)
        await asyncio.wait_for(context.__aenter__(), 2)
        try:
            async with asyncio.timeout(1):
                while not read.await_count:
                    await asyncio.sleep(.001)
            assert read.await_count, 'startup did not schedule nonblocking feed warmup'
        finally:
            await asyncio.wait_for(context.__aexit__(None, None, None), 2)
        assert not [t for t in asyncio.all_tasks() if t not in before and not t.done()]
        assert realtime_projection.stop_projection_queries.await_count == 1

    asyncio.run(check())
