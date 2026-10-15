from __future__ import annotations

import logging
import threading

import pytest
from prometheus_client import REGISTRY

from jetson_prom_exporter.collectors import JtopCollector, LabelTracker


def _sample_value(name: str, labels: dict[str, str]) -> float | None:
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name != name:
                continue
            if all(sample.labels.get(k) == v for k, v in labels.items()):
                return float(sample.value)
    return None


def _payload() -> dict:
    return {
        "board": {
            "platform": {"Machine": "aarch64"},
            "hardware": {"Model": "Jetson AGX Thor", "L4T": "38.2", "Jetpack": "7.0"},
            "libraries": {"CUDA": "13.0", "cuDNN": "9.8", "TensorRT": "10.8"},
        },
        "uptime": 123.0,
        "nvpmodel": "MAXN",
        "jetson_clocks": {"status": True, "boot": False},
        "cpu": {
            "cpu": [
                {
                    "online": True,
                    "governor": "schedutil",
                    "freq": {"cur": 1200, "min": 115, "max": 2200},
                    "user": 20.0,
                    "nice": 0.0,
                    "system": 5.0,
                    "idle": 75.0,
                    "model": "carmel",
                }
            ]
        },
        "memory": {
            "RAM": {"tot": 4096, "used": 1024, "free": 2048, "cached": 1024},
            "SWAP": {"tot": 2048, "used": 256, "cached": 64, "table": []},
            "EMC": {"val": 42.0, "cur": 1600, "min": 204, "max": 3200},
        },
        "gpu": {
            "igpu": {
                "status": {"load": 55.0, "railgate": False, "3d_scaling": True},
                "freq": {"cur": 900, "min": 300, "max": 1300, "GPC": [900, 901]},
            }
        },
        "engine": {"NVENC": {"NVENC0": {"online": True, "cur": 729, "min": 115, "max": 1290}}},
        "temperature": {"gpu": {"online": True, "temp": 62.5, "max": 95, "crit": 105}},
        "power": {
            "tot": {"power": 25000, "avg": 24000},
            "rail": {
                "VDD_GPU": {
                    "online": True,
                    "volt": 12000,
                    "curr": 500,
                    "power": 6000,
                    "avg": 5500,
                    "warn": 9000,
                    "crit": 10000,
                }
            },
        },
        "fan": {"pwm-fan": {"speed": [40], "rpm": [2500], "profile": "cool", "governor": "pid", "control": "auto"}},
        "disk": {"total": 128, "used": 64, "available": 60, "available_no_root": 58},
        "processes": [[1234, "robert", "I", "Graphic", 20, "R", 12.5, 2048, 1024, "demo"]],
    }


def test_update_from_payload_maps_jtop_metrics() -> None:
    JtopCollector.update_from_payload(_payload(), LabelTracker())

    assert _sample_value("jetson_jtop_cpu_utilization_percent", {"cpu": "0", "mode": "active"}) == 25.0
    assert _sample_value("jetson_jtop_cpu_frequency_hertz", {"cpu": "0", "state": "cur"}) == 1_200_000.0
    assert _sample_value("jetson_jtop_memory_bytes", {"memory": "ram", "state": "used"}) == 1_048_576.0
    assert _sample_value("jetson_jtop_gpu_load_percent", {"gpu": "igpu"}) == 55.0
    assert _sample_value("jetson_jtop_power_watts", {"rail": "total", "state": "current"}) == 25.0
    assert _sample_value("jetson_jtop_process_gpu_memory_bytes", {"pid": "1234", "process_name": "demo"}) == 1_048_576.0


def test_update_from_payload_removes_stale_labelled_metrics() -> None:
    tracker = LabelTracker()
    JtopCollector.update_from_payload(_payload(), tracker)
    assert _sample_value("jetson_jtop_fan_rpm", {"fan": "pwm-fan", "index": "0"}) == 2500.0

    missing_fan = _payload()
    missing_fan["fan"] = {}
    JtopCollector.update_from_payload(missing_fan, tracker)

    assert _sample_value("jetson_jtop_fan_rpm", {"fan": "pwm-fan", "index": "0"}) is None


class _StopRun(BaseException):
    pass


def _run_until(collector: JtopCollector, delays: list[float], attempts: int) -> None:
    def sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == attempts:
            raise _StopRun

    collector._sleep = sleep
    try:
        collector.run()
    except _StopRun:
        pass


def test_jtop_failures_back_off_to_a_cap_with_one_line_each(caplog: pytest.LogCaptureFixture) -> None:
    session_threads: list[threading.Thread] = []

    def unavailable(**_: object) -> object:
        session_threads.append(threading.current_thread())
        raise RuntimeError("The jtop.service is not active. Please run:\nsudo jtop --install-service")

    delays: list[float] = []
    with caplog.at_level(logging.WARNING, logger="jetson_prom_exporter.collectors"):
        _run_until(JtopCollector(jtop_factory=unavailable), delays, attempts=6)

    assert delays == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]
    # A fresh thread per session: jtop's manager proxies cache a dead
    # connection per thread after jtop.service restarts.
    assert len(set(map(id, session_threads))) == 6
    assert threading.current_thread() not in session_threads
    assert _sample_value("jetson_jtop_up", {}) == 0.0
    assert len(caplog.records) == 6
    record = caplog.records[0]
    assert record.exc_info is None
    assert record.getMessage() == (
        "jtop collection failed: RuntimeError: The jtop.service is not active. Please run:; retrying in 5s"
    )


def test_jtop_backoff_resets_after_a_session_that_sampled() -> None:
    outcomes = iter([False, False, True, False])

    class Flaky(JtopCollector):
        def _stream(self) -> None:
            self._sampled = next(outcomes)
            raise ConnectionError("lost")

    delays: list[float] = []
    _run_until(Flaky(), delays, attempts=4)

    assert delays == [5.0, 10.0, 5.0, 10.0]
