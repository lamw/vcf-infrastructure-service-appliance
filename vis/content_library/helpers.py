from typing import Any
from datetime import datetime, timezone

env_prefix = "VIS_CONTENT_LIB_"
epoch_zero = datetime.fromtimestamp(0, timezone.utc)
epoch_zero_str = epoch_zero.isoformat()


def empty(s: Any) -> bool:
    return not s


def xor(a: bool, b: bool) -> bool:
    return (a and not b) or (b and not a)
