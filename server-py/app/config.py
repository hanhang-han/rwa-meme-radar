"""Process-neutral environment loading."""
import os
from pathlib import Path


def bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Invalid optional settings fall back; operators cannot remove bounds."""
    try:
        value = int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        value = default
    return min(maximum, max(minimum, value))


def load_env() -> None:
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
