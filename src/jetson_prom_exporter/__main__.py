"""CLI entry point: ``python -m jetson_prom_exporter``."""

from __future__ import annotations

import argparse
import logging
import sys

from .exporter import run


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="jetson-prom-exporter",
        description="Prometheus exporter for NVIDIA Jetson (jtop + supplemental nvidia-smi dmon)",
    )
    parser.add_argument("--port", type=int, default=9101, help="HTTP port for /metrics (default: 9101)")
    parser.add_argument("--interval", type=int, default=30000, help="jtop polling interval in ms (default: 30000)")
    parser.add_argument("--nvidia-smi-path", default="/usr/bin/nvidia-smi", help="Path to nvidia-smi binary for dmon")
    parser.add_argument("--disable-nvidia-smi-dmon", action="store_true", help="Disable supplemental nvidia-smi dmon metrics")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )

    run(
        port=args.port,
        interval_ms=args.interval,
        nvidia_smi_path=args.nvidia_smi_path,
        enable_nvidia_smi_dmon=not args.disable_nvidia_smi_dmon,
    )


if __name__ == "__main__":
    main()
