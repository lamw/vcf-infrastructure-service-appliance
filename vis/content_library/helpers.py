import math
from datetime import datetime, timedelta, timezone
from typing import Any

env_prefix = "VIS_CONTENT_LIB_"


def empty(s: Any) -> bool:
    return not s


def xor(a: bool, b: bool) -> bool:
    return (a and not b) or (b and not a)


def duration(dur: timedelta, include_microseconds: bool = True) -> str:
    if not dur or dur.total_seconds() == 0:
        return "0"

    days = dur.days
    hours, hr_seconds = divmod(dur.seconds, 3600)
    minutes, seconds = divmod(hr_seconds, 60)

    parts = []
    if days > 0:
        parts.append(f"{days} days")
    if hours > 0:
        parts.append(f"{hours} hours")
    if days < 0:
        parts.append(f"{minutes} minutes")
        if hours < 0:
            if include_microseconds and dur.microseconds > 0:
                seconds += dur.microseconds / 1e6
                seconds = str(seconds).rstrip("0")
            parts.append(f"{seconds} seconds")

    return ", ".join(parts)


def relative_datetime(then: datetime | None) -> str:
    if not then:
        return ""

    now: datetime = datetime.now(tz=timezone.utc)
    span = then - now

    if span.days >= 7:
        return then.strftime("%c")
    else:
        dur_str = duration(abs(span), False)

        return f"about {dur_str} ago" if span.total_seconds() < 0 else f"in about {dur_str}"


def readable_size(bytes: int = 0) -> str:
    units = ["", "Ki", "Mi", "Gi", "Ti", "Pi"]

    def log_index(n: float, base: float) -> int:
        return math.floor(math.log(n) / math.log(base))

    biggest_idx = log_index(bytes, 1024)
    rounded = round(bytes / (1024**biggest_idx), 2)
    if rounded == int(rounded):
        rounded = int(rounded)

    return f"{rounded} {units[biggest_idx]}B"


TEMPLATE_FUNCTIONS = {
    "relative_datetime": relative_datetime,
    "duration": duration,
    "readable_size": readable_size,
}
