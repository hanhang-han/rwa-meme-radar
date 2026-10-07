"""Independent query service for the live-market lane (uvicorn port 8011)."""
import asyncio
from contextlib import asynccontextmanager

from .config import load_env

load_env()  # Database/lease paths must be configured before importing stores.

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .api import live_market
from .demand_leases import stop_lease_writer
from .live_market_store import close_all, configure_demand_leases, market_store


@asynccontextmanager
async def lifespan(_: FastAPI):
    # No original API import, catalogue connection, state/projection builder,
    # or research DDL is part of this service's startup/shutdown path.
    configure_demand_leases()
    await asyncio.wait_for(market_store('system'), 5)
    try:
        yield
    finally:
        await live_market.stop_market_hubs()
        await stop_lease_writer()
        await close_all()


app = FastAPI(title='Live Market Query API', lifespan=lifespan)
app.include_router(live_market.router, prefix='/api')


@app.get('/api/health/ready')
async def ready():
    try:
        scoped = await asyncio.wait_for(market_store('system'), 2)
        await asyncio.wait_for(scoped.fetchone('SELECT 1'), 1)
        return {'ok': True, 'service': 'pyradar-market-query', 'storage': 'ready',
                'mode': 'isolated-native-markets'}
    except Exception as error:
        return JSONResponse(status_code=503, content={'ok': False, 'service': 'pyradar-market-query',
                            'storage': 'unavailable', 'error': type(error).__name__})


@app.get('/api/health/live')
async def alive():
    return {'ok': True, 'service': 'pyradar-market-query'}
