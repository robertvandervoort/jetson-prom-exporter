"""Helpers for converting jtop payloads into Prometheus-friendly values."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
import re
from typing import Any

_LABEL_RE = re.compile(r"[^a-zA-Z0-9_]")


def as_mapping(value: Any) -> dict[str, Any]:
    """Return a normal dict for jtop objects that expose a mapping interface."""

    if isinstance(value, Mapping):
        return dict(value)
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def as_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, timedelta):
        return value.total_seconds()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def bool_to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "on", "online", "enabled", "enable", "active", "running"}:
            return 1.0
        if lowered in {"false", "off", "offline", "disabled", "disable", "inactive", "stopped"}:
            return 0.0
    return as_number(value)


def khz_to_hz(value: Any) -> float | None:
    number = as_number(value)
    return None if number is None else number * 1000.0


def kb_to_bytes(value: Any) -> float | None:
    number = as_number(value)
    return None if number is None else number * 1024.0


def gb_to_bytes(value: Any) -> float | None:
    number = as_number(value)
    return None if number is None else number * 1024.0 * 1024.0 * 1024.0


def mw_to_watts(value: Any) -> float | None:
    number = as_number(value)
    return None if number is None else number / 1000.0


def ma_to_amps(value: Any) -> float | None:
    number = as_number(value)
    return None if number is None else number / 1000.0


def mv_to_volts(value: Any) -> float | None:
    number = as_number(value)
    return None if number is None else number / 1000.0


def prom_label_name(value: str) -> str:
    """Normalize a jtop dictionary key into a valid Prometheus label name."""

    cleaned = _LABEL_RE.sub("_", value.strip()).strip("_").lower()
    if not cleaned:
        return "unknown"
    if cleaned[0].isdigit():
        return f"v_{cleaned}"
    return cleaned


def flatten_board_info(board: Mapping[str, Any]) -> dict[str, str]:
    """Flatten jtop board platform/hardware/library sections for an Info metric."""

    info: dict[str, str] = {}
    for section_name in ("platform", "hardware", "libraries"):
        section = as_mapping(board.get(section_name))
        for key, value in section.items():
            if value is None:
                continue
            info[f"{prom_label_name(section_name)}_{prom_label_name(str(key))}"] = str(value)
    return info


def capture_jtop_payload(jetson: Any) -> dict[str, Any]:
    """Copy the jtop properties the exporter maps, recursively enough for tests."""

    payload: dict[str, Any] = {}
    for name in (
        "board",
        "cpu",
        "memory",
        "gpu",
        "engine",
        "fan",
        "temperature",
        "power",
        "processes",
        "jetson_clocks",
        "nvpmodel",
        "disk",
        "uptime",
        "local_interfaces",
    ):
        try:
            payload[name] = to_plain_value(getattr(jetson, name))
        except Exception:
            payload[name] = None
    return payload


def to_plain_value(value: Any) -> Any:
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Mapping):
        return {str(k): to_plain_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain_value(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    mapped = as_mapping(value)
    if mapped:
        return {str(k): to_plain_value(v) for k, v in mapped.items()}
    return str(value)
