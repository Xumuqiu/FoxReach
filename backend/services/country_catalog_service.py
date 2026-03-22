import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _load_catalog() -> list[dict]:
    path = Path(__file__).resolve().parents[1] / "static" / "country_catalog.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def list_countries() -> list[dict]:
    return _load_catalog()


def get_default_time_zone(country_code: str | None) -> str | None:
    if not country_code:
        return None
    code = country_code.strip().upper()
    for item in _load_catalog():
        if str(item.get("code", "")).upper() == code:
            return item.get("default_time_zone") or (item.get("time_zones") or [None])[0]
    return None

