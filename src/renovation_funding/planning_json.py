"""门禁模块共享的确定性 JSON 与数值辅助。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
