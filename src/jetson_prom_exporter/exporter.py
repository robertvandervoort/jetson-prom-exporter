"""HTTP server exposing /metrics for Prometheus scraping."""

from __future__ import annotations

import logging

from prometheus_client import start_http_server

from .collectors import JtopCollector, NvidiaSmiCollector

log = logging.getLogger(__name__)


def run(
    port: int = 9101,
    interval_ms: int = 30000,
    nvidia_smi_path: str = "/usr/bin/nvidia-smi",
    enable_nvidia_smi_dmon: bool = True,
) -> None:
    """Start the collector threads and Prometheus HTTP server."""

    log.info(
        "jetson-prom-exporter starting — port=%d interval=%dms jtop=true nvidia-smi-dmon=%s",
        port, interval_ms, enable_nvidia_smi_dmon,
    )

    jtop = JtopCollector(
        interval_ms=interval_ms,
    )

    jtop.start()
    if enable_nvidia_smi_dmon:
        smi = NvidiaSmiCollector(
            interval_sec=interval_ms / 1000.0,
            nvidia_smi_path=nvidia_smi_path,
        )
        smi.start()

    start_http_server(port)
    log.info("Serving metrics on :%d/metrics", port)

    jtop.join()
