"""Parse nvidia-smi CSV and dmon output into structured dicts."""

from __future__ import annotations

import re
from typing import Any


def parse_query_gpu(csv_output: str) -> dict[str, Any]:
    """Parse ``nvidia-smi --query-gpu=... --format=csv`` output.

    Expects a two-line CSV (header + one data row).  Fields that return
    ``[N/A]`` or ``N/A`` are stored as None.
    """
    lines = [l.strip() for l in csv_output.strip().splitlines() if l.strip()]
    if len(lines) < 2:
        return {}

    headers = [h.strip() for h in lines[0].split(",")]
    values = [v.strip() for v in lines[1].split(",")]

    result: dict[str, Any] = {}
    for hdr, val in zip(headers, values):
        key = _header_to_key(hdr)
        result[key] = _clean_value(val)
    return result


def parse_query_apps(csv_output: str) -> list[dict[str, Any]]:
    """Parse ``nvidia-smi --query-compute-apps=... --format=csv`` output.

    Returns a list of dicts, one per GPU process.
    """
    lines = [l.strip() for l in csv_output.strip().splitlines() if l.strip()]
    if len(lines) < 2:
        return []

    headers = [h.strip() for h in lines[0].split(",")]
    rows: list[dict[str, Any]] = []
    for line in lines[1:]:
        values = [v.strip() for v in line.split(",")]
        row: dict[str, Any] = {}
        for hdr, val in zip(headers, values):
            key = _header_to_key(hdr)
            row[key] = _clean_value(val)
        rows.append(row)
    return rows


def parse_dmon(dmon_output: str) -> dict[str, Any]:
    """Parse ``nvidia-smi dmon -s pucem -c 1`` output.

    Returns dict with keys matching dmon column names.  ``-`` values become None.
    """
    lines = [l for l in dmon_output.strip().splitlines() if not l.startswith("# Idx") and l.strip()]

    header_line = None
    data_line = None
    for l in dmon_output.strip().splitlines():
        if l.startswith("# gpu"):
            header_line = l
        elif not l.startswith("#") and l.strip():
            data_line = l

    if not header_line or not data_line:
        return {}

    headers = header_line.lstrip("# ").split()
    values = data_line.split()

    result: dict[str, Any] = {}
    for hdr, val in zip(headers, values):
        if val == "-":
            result[hdr] = None
        else:
            try:
                result[hdr] = int(val)
            except ValueError:
                result[hdr] = val
    return result


_UNIT_RE = re.compile(r"\s*\[.*\]$")


def _header_to_key(header: str) -> str:
    """Normalize a CSV header like 'temperature.gpu' or 'power.draw [W]'."""
    key = _UNIT_RE.sub("", header).strip()
    key = key.replace(".", "_").replace(" ", "_")
    return key


def _clean_value(val: str) -> int | float | str | None:
    """Convert a CSV cell to a Python value."""
    if val in ("[N/A]", "N/A", "[Not Supported]", "N/A W", "N/A %"):
        return None
    # Strip unit suffixes: "1.98 W" -> "1.98", "40 %" -> "40"
    stripped = re.sub(r"\s*[WM%]i?[Bb]?\s*$", "", val).strip()
    if not stripped:
        return None
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        pass
    return val
