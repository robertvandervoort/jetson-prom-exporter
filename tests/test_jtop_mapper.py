from __future__ import annotations

from datetime import timedelta

from jetson_prom_exporter.jtop_mapper import (
    bool_to_float,
    capture_jtop_payload,
    flatten_board_info,
    gb_to_bytes,
    khz_to_hz,
    kb_to_bytes,
    mw_to_watts,
    to_plain_value,
)


class FakeJetson:
    board = {
        "platform": {"Machine": "aarch64"},
        "hardware": {"Model": "Jetson AGX Thor", "L4T": "38.2"},
        "libraries": {"CUDA": "13.0", "TensorRT": "10.8"},
    }
    cpu = {"cpu": []}
    memory = {}
    gpu = {}
    engine = {}
    fan = {}
    temperature = {}
    power = {}
    processes = []
    jetson_clocks = {"status": True, "boot": False}
    nvpmodel = "MAXN"
    disk = {"total": 128}
    uptime = 42.0
    local_interfaces = {"hostname": "chip", "interfaces": {"eth0": ["192.168.50.1"]}}


def test_unit_conversions() -> None:
    assert khz_to_hz(1200) == 1_200_000
    assert kb_to_bytes(1024) == 1_048_576
    assert gb_to_bytes(2) == 2_147_483_648
    assert mw_to_watts(2500) == 2.5
    assert to_plain_value(timedelta(days=1, seconds=2)) == 86_402.0


def test_bool_to_float_handles_jtop_strings() -> None:
    assert bool_to_float(True) == 1.0
    assert bool_to_float(False) == 0.0
    assert bool_to_float("online") == 1.0
    assert bool_to_float("disabled") == 0.0


def test_flatten_board_info_normalizes_label_names() -> None:
    info = flatten_board_info(FakeJetson.board)

    assert info["platform_machine"] == "aarch64"
    assert info["hardware_model"] == "Jetson AGX Thor"
    assert info["libraries_cuda"] == "13.0"


def test_capture_jtop_payload_reads_expected_properties() -> None:
    payload = capture_jtop_payload(FakeJetson())

    assert payload["board"]["libraries"]["CUDA"] == "13.0"
    assert payload["jetson_clocks"]["status"] is True
    assert payload["local_interfaces"]["hostname"] == "chip"
