"""Aggregated snapshot endpoint. Placeholder until collectors land (P1)."""
from fastapi import APIRouter

router = APIRouter()


@router.get("/dashboard")
async def get_dashboard():
    return {
        "now": 0,
        "unified": {
            "version": 2,
            "sources": [],
            "assets": [],
            "stockTokens": [],
            "relations": [],
            "signals": [],
            "groups": [],
            "quality": {},
            "metrics": {},
            "sectors": [],
            "distribution": [],
            "capabilities": [],
            "collection": {},
        },
    }
