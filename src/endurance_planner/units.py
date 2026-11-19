"""Parsing/formatting of clock-style values (paces in min/km, race times)."""


def parse_clock(value: object) -> int:
    """Parse 'm:ss' or 'h:mm:ss' into seconds.

    Plain numbers are taken as seconds (YAML 1.1 reads an unquoted 4:15 as the
    sexagesimal integer 255, which is the same value).
    """
    if isinstance(value, int | float) and not isinstance(value, bool):
        return round(value)
    if isinstance(value, str):
        parts = value.strip().split(":")
        if len(parts) in (2, 3) and all(p.isdigit() for p in parts):
            seconds = 0
            for part in parts:
                seconds = seconds * 60 + int(part)
            return seconds
    raise ValueError(f"expected 'm:ss' or 'h:mm:ss', got {value!r}")


def format_clock(seconds: float) -> str:
    """Format seconds as 'm:ss' (or 'h:mm:ss' from one hour up)."""
    hours, rest = divmod(round(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def format_duration(seconds: int) -> str:
    """Compact duration: 45s, 12m, 1m30s, 1h30m."""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    text = (f"{hours}h" if hours else "") + (f"{minutes}m" if minutes else "")
    return text + (f"{secs}s" if secs or not text else "")
