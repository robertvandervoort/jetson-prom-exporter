from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from prometheus_client import REGISTRY

from jetson_prom_exporter.host_connectivity import (
    HostConnectivityCollector,
    LinkPeer,
    discover_interfaces,
    parse_iw_link,
    parse_link_peers,
)


def _sample_value(name: str, labels: dict[str, str]) -> float | None:
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name == name and all(sample.labels.get(key) == value for key, value in labels.items()):
                return float(sample.value)
    return None


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


@pytest.fixture
def host_sysfs(tmp_path: Path) -> Path:
    root = tmp_path / "sys" / "class" / "net"
    ethernet = root / "eth0"
    wifi = root / "wlan0"
    can = root / "can0"
    usb = root / "usb0"
    (ethernet / "device").mkdir(parents=True)
    (wifi / "wireless").mkdir(parents=True)
    (can / "device").mkdir(parents=True)
    (usb / "device").mkdir(parents=True)
    (root / "lo").mkdir(parents=True)
    _write(ethernet / "type", "1")  # ARPHRD_ETHER
    _write(can / "type", "280")  # ARPHRD_CAN
    _write(usb / "type", "1")
    for interface, carrier, state, speed, duplex in (
        (ethernet, "1", "up", "1000", "full"),
        (wifi, "1", "up", "866", "full"),
        (can, "1", "up", "1000", "full"),
        (usb, "1", "up", "100", "full"),
    ):
        _write(interface / "carrier", carrier)
        _write(interface / "operstate", state)
        _write(interface / "speed", speed)
        _write(interface / "duplex", duplex)
        for name, value in {
            "rx_bytes": "100",
            "tx_bytes": "200",
            "rx_errors": "3",
            "tx_errors": "4",
            "rx_dropped": "5",
            "tx_dropped": "6",
        }.items():
            _write(interface / "statistics" / name, value)
    return root


@pytest.fixture
def command_runner():
    def run(
        args: list[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        del capture_output, text, timeout, check
        if args[:3] == ["iw", "dev", "wlan0"]:
            return subprocess.CompletedProcess(
                args,
                0,
                """Connected to 00:11:22:33:44:55 (on wlan0)
\tSSID: Chip Lab Full SSID
\tfreq: 5180
\tsignal: -61 dBm
\ttx bitrate: 432.1 MBit/s
\trx bitrate: 390.0 MBit/s
""",
                "",
            )
        if args[0] == "ping":
            return subprocess.CompletedProcess(
                args,
                0,
                "64 bytes from 192.168.50.1: icmp_seq=1 ttl=64 time=0.421 ms\n",
                "",
            )
        raise AssertionError(args)

    return run


@pytest.fixture
def bluez_collector():
    async def collect() -> dict:
        return {
            "adapters": [{"adapter": "hci0", "powered": True}],
            "devices": [
                {
                    "adapter": "hci0",
                    "address": "AA:BB:CC:DD:EE:FF",
                    "name": "Chip Controller",
                    "connected": True,
                    "rssi": -48,
                    "battery": 73,
                }
            ],
        }

    return collect


def test_parse_link_peers_is_bounded_and_validated() -> None:
    assert parse_link_peers("thor=192.168.50.1,chip-nano") == (
        LinkPeer("thor", "192.168.50.1"),
        LinkPeer("chip-nano", "chip-nano"),
    )
    with pytest.raises(ValueError, match="invalid"):
        parse_link_peers("bad peer=192.168.50.1")
    with pytest.raises(ValueError, match="unique"):
        parse_link_peers("thor=192.168.50.1,thor=192.168.50.2")
    with pytest.raises(ValueError, match="at most 32"):
        parse_link_peers(",".join(f"peer-{number}=10.0.0.{number}" for number in range(33)))


def test_discover_interfaces_classifies_wifi_and_physical_ethernet(host_sysfs: Path) -> None:
    assert [(name, kind) for name, kind, _ in discover_interfaces(host_sysfs)] == [
        ("eth0", "ethernet"),
        ("wlan0", "wifi"),
    ]


def test_discover_interfaces_skips_can_usb_and_virtual_names(host_sysfs: Path) -> None:
    names = {name for name, _kind, _path in discover_interfaces(host_sysfs)}
    assert "can0" not in names
    assert "usb0" not in names
    assert "lo" not in names


def test_parse_iw_link_preserves_full_ssid_and_link_values() -> None:
    parsed = parse_iw_link(
        """Connected to 00:11:22:33:44:55 (on wlan0)
\tSSID: A full SSID with spaces
\tfreq: 2412
\tsignal: -55 dBm
\ttx bitrate: 72.2 MBit/s
\trx bitrate: 65.0 MBit/s
"""
    )
    assert parsed == {
        "connected": True,
        "ssid": "A full SSID with spaces",
        "frequency_hz": 2_412_000_000.0,
        "signal_dbm": -55.0,
        "quality_percent": 90.0,
        "tx_bitrate_bps": 72_200_000.0,
        "rx_bitrate_bps": 65_000_000.0,
    }
    assert parse_iw_link("Not connected.\n") == {"connected": False}


def test_collect_once_exports_all_connectivity_sources(
    host_sysfs: Path,
    command_runner,
    bluez_collector,
) -> None:
    collector = HostConnectivityCollector(
        sys_class_net=host_sysfs,
        peers=[LinkPeer("thor", "192.168.50.1")],
        run_command=command_runner,
        bluez_collector=bluez_collector,
    )
    collector.collect_once()

    assert _sample_value(
        "jetson_host_network_interface_carrier",
        {"interface": "eth0", "type": "ethernet"},
    ) == 1
    assert _sample_value(
        "jetson_host_network_interface_speed_bits_per_second",
        {"interface": "eth0", "type": "ethernet"},
    ) == 1_000_000_000
    assert _sample_value(
        "jetson_host_network_interface_errors_total",
        {"interface": "eth0", "type": "ethernet", "direction": "receive"},
    ) == 3
    assert _sample_value("jetson_host_wifi_info", {"interface": "wlan0", "ssid": "Chip Lab Full SSID"}) == 1
    assert _sample_value("jetson_host_wifi_signal_dbm", {"interface": "wlan0"}) == -61
    assert _sample_value(
        "jetson_host_bluetooth_device_connected",
        {"adapter": "hci0", "address": "AA:BB:CC:DD:EE:FF"},
    ) == 1
    assert _sample_value(
        "jetson_host_bluetooth_device_battery_percent",
        {"adapter": "hci0", "address": "AA:BB:CC:DD:EE:FF"},
    ) == 73
    assert _sample_value(
        "jetson_host_link_peer_rtt_seconds",
        {"peer": "thor", "address": "192.168.50.1"},
    ) == pytest.approx(0.000421)
    assert _sample_value("jetson_host_connectivity_collector_up", {}) == 1
    for source in ("sysfs", "wifi", "bluetooth", "peers"):
        assert _sample_value("jetson_host_connectivity_source_up", {"source": source}) == 1


def test_failed_source_sets_health_without_losing_other_metrics(
    host_sysfs: Path,
    command_runner,
) -> None:
    async def failed_bluez() -> dict:
        raise RuntimeError("D-Bus unavailable")

    collector = HostConnectivityCollector(
        sys_class_net=host_sysfs,
        run_command=command_runner,
        bluez_collector=failed_bluez,
    )
    collector.collect_once()

    assert _sample_value("jetson_host_connectivity_collector_up", {}) == 0
    assert _sample_value("jetson_host_connectivity_source_up", {"source": "bluetooth"}) == 0
    assert _sample_value("jetson_host_connectivity_source_up", {"source": "sysfs"}) == 1
    assert _sample_value(
        "jetson_host_network_interface_bytes_total",
        {"interface": "eth0", "type": "ethernet", "direction": "transmit"},
    ) == 200

