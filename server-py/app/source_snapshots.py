"""A projection reads shared producer files from its pinned database snapshot."""
from contextlib import contextmanager
from contextvars import ContextVar

_snapshots = ContextVar('projection_source_snapshots', default=None)


def pinned_snapshot(path):
    sources = _snapshots.get()
    return None if sources is None else sources.get(path, {})


@contextmanager
def bind_snapshots(sources):
    token = _snapshots.set(sources)
    try:
        yield
    finally:
        _snapshots.reset(token)
