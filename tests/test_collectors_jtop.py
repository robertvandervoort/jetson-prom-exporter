from __future__ import annotations

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
        "local_interfaces": {"hostname": "chip", "interfaces": {"eth0": ["192.168.50.1"]}},
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
