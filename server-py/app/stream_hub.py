"""SSE fan-out hub: collectors broadcast, /api/stream drains to clients."""
import json
import time

_clients: set = set()


def broadcast(event: str, data) -> None:
    if not _clients:
        return
    frame = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
    for q in list(_clients):
        try:
            q.put_nowait(frame)
        except Exception:
            _clients.discard(q)


def clients() -> set:
    return _clients


def hello(q) -> None:
    q.put_nowait(f"event: hello\ndata: {json.dumps({'at': int(time.time() * 1000)})}\n\n".encode())
