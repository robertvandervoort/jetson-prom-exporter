"""Host network, Wi-Fi, Bluetooth, and configured peer connectivity metrics."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from prometheus_client import Counter, Gauge

log = logging.getLogger(__name__)

COLLECTOR_UP = Gauge(
    "jetson_host_connectivity_collector_up",
    "Whether the most recent host connectivity collection completed",
)
SOURCE_UP = Gauge(
    "jetson_host_connectivity_source_up",
    "Whether a host connectivity source completed successfully",
    ["source"],
)
COLLECTION_ERRORS = Counter(
    "jetson_host_connectivity_collection_errors_total",
    "Host connectivity collection failures",
    ["source"],
)
LAST_SUCCESS = Gauge(
    "jetson_host_connectivity_last_success_timestamp_seconds",
    "Unix timestamp of the last successful host connectivity collection",
)

INTERFACE_INFO = Gauge(
    "jetson_host_network_interface_info",
    "Discovered physical host network interfaces",
    ["interface", "type"],
)
INTERFACE_CARRIER = Gauge(
    "jetson_host_network_interface_carrier",
    "Physical link carrier state",
    ["interface", "type"],
)
INTERFACE_OPERSTATE = Gauge(
    "jetson_host_network_interface_operstate_info",
    "Interface operational state",
    ["interface", "type", "state"],
)
INTERFACE_SPEED = Gauge(
    "jetson_host_network_interface_speed_bits_per_second",
    "Negotiated interface speed",
    ["interface", "type"],
)
INTERFACE_DUPLEX = Gauge(
    "jetson_host_network_interface_duplex_info",
    "Negotiated interface duplex mode",
    ["interface", "type", "duplex"],
)
INTERFACE_BYTES = Gauge(
    "jetson_host_network_interface_bytes_total",
    "Host interface byte counter",
    ["interface", "type", "direction"],
)
INTERFACE_ERRORS = Gauge(
    "jetson_host_network_interface_errors_total",
    "Host interface error counter",
    ["interface", "type", "direction"],
)
INTERFACE_DROPS = Gauge(
    "jetson_host_network_interface_dropped_total",
    "Host interface dropped packet counter",
    ["interface", "type", "direction"],
)

WIFI_CONNECTED = Gauge(
    "jetson_host_wifi_connected",
    "Whether a Wi-Fi interface is associated",
    ["interface"],
)
WIFI_INFO = Gauge(
    "jetson_host_wifi_info",
    "Associated Wi-Fi network information",
    ["interface", "ssid"],
)
WIFI_SIGNAL_DBM = Gauge(
    "jetson_host_wifi_signal_dbm",
    "Wi-Fi received signal strength in dBm",
    ["interface"],
)
WIFI_QUALITY = Gauge(
    "jetson_host_wifi_quality_percent",
    "Wi-Fi signal quality derived from signal strength",
    ["interface"],
)
WIFI_TX_BITRATE = Gauge(
    "jetson_host_wifi_tx_bitrate_bits_per_second",
    "Current Wi-Fi transmit bitrate",
    ["interface"],
)
WIFI_RX_BITRATE = Gauge(
    "jetson_host_wifi_rx_bitrate_bits_per_second",
    "Current Wi-Fi receive bitrate",
    ["interface"],
)
WIFI_FREQUENCY = Gauge(
    "jetson_host_wifi_frequency_hertz",
    "Current Wi-Fi center frequency",
    ["interface"],
)

BLUETOOTH_ADAPTER_UP = Gauge(
    "jetson_host_bluetooth_adapter_up",
    "Whether a BlueZ adapter is powered",
    ["adapter"],
)
BLUETOOTH_DEVICE_INFO = Gauge(
    "jetson_host_bluetooth_device_info",
    "BlueZ device identity",
    ["adapter", "address", "name"],
)
BLUETOOTH_DEVICE_CONNECTED = Gauge(
    "jetson_host_bluetooth_device_connected",
    "Whether a BlueZ device is connected",
    ["adapter", "address"],
)
BLUETOOTH_DEVICE_RSSI_DBM = Gauge(
    "jetson_host_bluetooth_device_rssi_dbm",
    "BlueZ device RSSI when exposed",
    ["adapter", "address"],
)
BLUETOOTH_DEVICE_BATTERY_PERCENT = Gauge(
    "jetson_host_bluetooth_device_battery_percent",
    "BlueZ device battery percentage when exposed",
    ["adapter", "address"],
)

PEER_REACHABLE = Gauge(
    "jetson_host_link_peer_reachable",
    "Whether a configured peer answered ICMP",
    ["peer", "address"],
)
PEER_RTT = Gauge(
    "jetson_host_link_peer_rtt_seconds",
    "ICMP round-trip time to a configured peer",
    ["peer", "address"],
)

_SOURCES = ("sysfs", "wifi", "bluetooth", "peers")
_OPERSTATES = {"unknown", "notpresent", "down", "lowerlayerdown", "testing", "dormant", "up"}
_DUPLEXES = {"full", "half", "unknown"}
_PING_RTT_RE = re.compile(r"time[=<]([\d.]+)\s*ms")
_IW_VALUE_RE = re.compile(r"^\s*([^:]+):\s*(.*)$")
_SAFE_PEER_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SAFE_ADDRESS_RE = re.compile(r"^[A-Za-z0-9:._-]{1,253}$")


@dataclass(frozen=True)
class LinkPeer:
    name: str
    address: str


def parse_link_peers(raw: str | None) -> tuple[LinkPeer, ...]:
    """Parse comma-separated ``name=address`` peers, bounded to 32 entries."""
    peers: list[LinkPeer] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, separator, address = item.partition("=")
        if not separator:
            address = name
        name = name.strip()
        address = address.strip()
        if not _SAFE_PEER_NAME_RE.fullmatch(name) or not _SAFE_ADDRESS_RE.fullmatch(address):
            raise ValueError(f"invalid CHIP_LINK_PEERS entry: {item!r}")
        peers.append(LinkPeer(name=name, address=address))
        if len(peers) > 32:
            raise ValueError("CHIP_LINK_PEERS supports at most 32 peers")
    if len({peer.name for peer in peers}) != len(peers):
        raise ValueError("CHIP_LINK_PEERS peer names must be unique")
    return tuple(peers)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None


def _read_float(path: Path, multiplier: float = 1.0) -> float | None:
    value = _read_text(path)
    try:
        return float(value) * multiplier if value is not None else None
    except ValueError:
        return None


# ARPHRD_* values from Linux if_arp.h. Keep Wi-Fi + Ethernet; drop CAN/USB gadget/etc.
_ARPHRD_ETHER = 1
_SKIP_INTERFACE_PREFIXES = (
    "can",
    "usb",
    "veth",
    "docker",
    "br-",
    "virbr",
    "tun",
    "tap",
    "wg",
    "dummy",
    "sit",
    "nlmon",
    "ipvlan",
    "macvlan",
)


def _is_skipped_interface_name(name: str) -> bool:
    return name == "lo" or any(name.startswith(prefix) for prefix in _SKIP_INTERFACE_PREFIXES)


def discover_interfaces(sys_class_net: Path) -> list[tuple[str, str, Path]]:
    """Return Wi-Fi and physical Ethernet interfaces from host sysfs."""
    interfaces: list[tuple[str, str, Path]] = []
    if not sys_class_net.exists():
        raise FileNotFoundError(sys_class_net)
    for interface_path in sorted(sys_class_net.iterdir(), key=lambda path: path.name):
        if _is_skipped_interface_name(interface_path.name):
            continue
        if (interface_path / "wireless").exists() or (interface_path / "phy80211").exists():
            interfaces.append((interface_path.name, "wifi", interface_path))
            continue
        if not (interface_path / "device").exists():
            continue
        arphrd = _read_float(interface_path / "type")
        if arphrd is not None and int(arphrd) != _ARPHRD_ETHER:
            continue
        interfaces.append((interface_path.name, "ethernet", interface_path))
    return interfaces


def parse_iw_link(output: str) -> dict[str, Any]:
    if not output.strip() or "Not connected." in output:
        return {"connected": False}
    result: dict[str, Any] = {"connected": "Connected to " in output}
    for line in output.splitlines():
        match = _IW_VALUE_RE.match(line)
        if not match:
            continue
        key, value = match.groups()
        key = key.strip().lower()
        value = value.strip()
        if key == "ssid":
            result["ssid"] = value
        elif key == "signal":
            number = re.search(r"-?[\d.]+", value)
            if number:
                result["signal_dbm"] = float(number.group())
        elif key in {"tx bitrate", "rx bitrate"}:
            number = re.search(r"[\d.]+", value)
            if number:
                result[key.replace(" ", "_") + "_bps"] = float(number.group()) * 1_000_000
        elif key == "freq":
            number = re.search(r"[\d.]+", value)
            if number:
                result["frequency_hz"] = float(number.group()) * 1_000_000
    signal = result.get("signal_dbm")
    if signal is not None:
        result["quality_percent"] = max(0.0, min(100.0, 2.0 * (float(signal) + 100.0)))
    return result


def _unwrap(value: Any) -> Any:
    return getattr(value, "value", value)


async def collect_bluez() -> dict[str, list[dict[str, Any]]]:
    """Read BlueZ ObjectManager data from the host system D-Bus."""
    from dbus_next.aio import MessageBus
    from dbus_next.constants import BusType

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        introspection = await bus.introspect("org.bluez", "/")
        proxy = bus.get_proxy_object("org.bluez", "/", introspection)
        manager = proxy.get_interface("org.freedesktop.DBus.ObjectManager")
        objects = await manager.call_get_managed_objects()
    finally:
        bus.disconnect()

    adapters: list[dict[str, Any]] = []
    devices: list[dict[str, Any]] = []
    for path, interfaces in objects.items():
        adapter = interfaces.get("org.bluez.Adapter1")
        if adapter:
            adapters.append(
                {
                    "path": path,
                    "adapter": path.rsplit("/", 1)[-1],
                    "powered": bool(_unwrap(adapter.get("Powered", False))),
                }
            )
        device = interfaces.get("org.bluez.Device1")
        if not device:
            continue
        adapter_path = str(_unwrap(device.get("Adapter", "")))
        record = {
            "adapter": adapter_path.rsplit("/", 1)[-1],
            "address": str(_unwrap(device.get("Address", ""))),
            "name": str(_unwrap(device.get("Name", device.get("Alias", "")))),
            "connected": bool(_unwrap(device.get("Connected", False))),
        }
        if "RSSI" in device:
            record["rssi"] = float(_unwrap(device["RSSI"]))
        battery = interfaces.get("org.bluez.Battery1")
        if battery and "Percentage" in battery:
            record["battery"] = float(_unwrap(battery["Percentage"]))
        devices.append(record)
    return {"adapters": adapters, "devices": devices}


class HostConnectivityCollector(threading.Thread):
    """Periodically collect connectivity directly from host facilities."""

    def __init__(
        self,
        interval_sec: float = 30.0,
        sys_class_net: Path = Path("/sys/class/net"),
        peers: Iterable[LinkPeer] = (),
        run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        bluez_collector: Callable[[], Awaitable[dict[str, list[dict[str, Any]]]]] = collect_bluez,
    ):
        super().__init__(daemon=True, name="host-connectivity-collector")
        self.interval_sec = interval_sec
        self.sys_class_net = sys_class_net
        self.peers = tuple(peers)
        self._run_command = run_command
        self._bluez_collector = bluez_collector
        self._known_labels: dict[Gauge, set[tuple[str, ...]]] = {}

    @classmethod
    def from_environment(cls, interval_sec: float = 30.0) -> "HostConnectivityCollector":
        return cls(interval_sec=interval_sec, peers=parse_link_peers(os.getenv("CHIP_LINK_PEERS")))

    def run(self) -> None:
        while True:
            started = time.monotonic()
            self.collect_once()
            time.sleep(max(0.0, self.interval_sec - (time.monotonic() - started)))

    def _record_source(self, source: str, operation: Callable[[], None]) -> bool:
        try:
            operation()
        except Exception:
            SOURCE_UP.labels(source=source).set(0)
            COLLECTION_ERRORS.labels(source=source).inc()
            log.exception("host connectivity source failed: %s", source)
            return False
        SOURCE_UP.labels(source=source).set(1)
        return True

    def collect_once(self) -> None:
        observed: dict[Gauge, set[tuple[str, ...]]] = {}
        interfaces: list[tuple[str, str, Path]] = []

        def collect_sysfs() -> None:
            nonlocal interfaces
            interfaces = discover_interfaces(self.sys_class_net)
            self._collect_interfaces(interfaces, observed)

        results = [
            self._record_source("sysfs", collect_sysfs),
            self._record_source("wifi", lambda: self._collect_wifi(interfaces, observed)),
            self._record_source("bluetooth", lambda: self._collect_bluetooth(observed)),
            self._record_source("peers", lambda: self._collect_peers(observed)),
        ]
        self._reconcile(observed)
        complete = all(results)
        COLLECTOR_UP.set(float(complete))
        if complete:
            LAST_SUCCESS.set(time.time())

    def _set(
        self,
        observed: dict[Gauge, set[tuple[str, ...]]],
        gauge: Gauge,
        labels: tuple[str, ...],
        value: float,
    ) -> None:
        gauge.labels(*labels).set(value)
        observed.setdefault(gauge, set()).add(labels)

    def _collect_interfaces(
        self,
        interfaces: list[tuple[str, str, Path]],
        observed: dict[Gauge, set[tuple[str, ...]]],
    ) -> None:
        for interface, kind, path in interfaces:
            labels = (interface, kind)
            self._set(observed, INTERFACE_INFO, labels, 1)
            carrier = _read_float(path / "carrier")
            if carrier is not None:
                self._set(observed, INTERFACE_CARRIER, labels, carrier)
            state = (_read_text(path / "operstate") or "unknown").lower()
            state = state if state in _OPERSTATES else "unknown"
            self._set(observed, INTERFACE_OPERSTATE, labels + (state,), 1)
            speed = _read_float(path / "speed", 1_000_000)
            if speed is not None and speed >= 0:
                self._set(observed, INTERFACE_SPEED, labels, speed)
            duplex = (_read_text(path / "duplex") or "unknown").lower()
            duplex = duplex if duplex in _DUPLEXES else "unknown"
            self._set(observed, INTERFACE_DUPLEX, labels + (duplex,), 1)
            for direction, prefix in (("receive", "rx"), ("transmit", "tx")):
                for gauge, suffix in (
                    (INTERFACE_BYTES, "bytes"),
                    (INTERFACE_ERRORS, "errors"),
                    (INTERFACE_DROPS, "dropped"),
                ):
                    value = _read_float(path / "statistics" / f"{prefix}_{suffix}")
                    if value is not None:
                        self._set(observed, gauge, labels + (direction,), value)

    def _collect_wifi(
        self,
        interfaces: list[tuple[str, str, Path]],
        observed: dict[Gauge, set[tuple[str, ...]]],
    ) -> None:
        for interface, kind, _path in interfaces:
            if kind != "wifi":
                continue
            result = self._run_command(
                ["iw", "dev", interface, "link"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(f"iw failed for {interface}: {result.stderr[:200]}")
            link = parse_iw_link(result.stdout)
            self._set(observed, WIFI_CONNECTED, (interface,), float(link["connected"]))
            if not link["connected"]:
                continue
            self._set(observed, WIFI_INFO, (interface, str(link.get("ssid", ""))), 1)
            for key, gauge in (
                ("signal_dbm", WIFI_SIGNAL_DBM),
                ("quality_percent", WIFI_QUALITY),
                ("tx_bitrate_bps", WIFI_TX_BITRATE),
                ("rx_bitrate_bps", WIFI_RX_BITRATE),
                ("frequency_hz", WIFI_FREQUENCY),
            ):
                if key in link:
                    self._set(observed, gauge, (interface,), float(link[key]))

    def _collect_bluetooth(self, observed: dict[Gauge, set[tuple[str, ...]]]) -> None:
        payload = asyncio.run(asyncio.wait_for(self._bluez_collector(), timeout=10))
        for adapter in payload["adapters"]:
            self._set(
                observed,
                BLUETOOTH_ADAPTER_UP,
                (str(adapter["adapter"]),),
                float(adapter["powered"]),
            )
        for device in payload["devices"]:
            labels = (str(device["adapter"]), str(device["address"]))
            self._set(
                observed,
                BLUETOOTH_DEVICE_INFO,
                labels + (str(device["name"]),),
                1,
            )
            self._set(
                observed,
                BLUETOOTH_DEVICE_CONNECTED,
                labels,
                float(device["connected"]),
            )
            if "rssi" in device:
                self._set(observed, BLUETOOTH_DEVICE_RSSI_DBM, labels, float(device["rssi"]))
            if "battery" in device:
                self._set(observed, BLUETOOTH_DEVICE_BATTERY_PERCENT, labels, float(device["battery"]))

    def _collect_peers(self, observed: dict[Gauge, set[tuple[str, ...]]]) -> None:
        for peer in self.peers:
            result = self._run_command(
                ["ping", "-n", "-c", "1", "-W", "1", peer.address],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            labels = (peer.name, peer.address)
            reachable = result.returncode == 0
            self._set(observed, PEER_REACHABLE, labels, float(reachable))
            if reachable:
                match = _PING_RTT_RE.search(result.stdout)
                if match:
                    self._set(observed, PEER_RTT, labels, float(match.group(1)) / 1000)

    def _reconcile(self, observed: dict[Gauge, set[tuple[str, ...]]]) -> None:
        for gauge in set(self._known_labels) | set(observed):
            current = observed.get(gauge, set())
            for labels in self._known_labels.get(gauge, set()) - current:
                try:
                    gauge.remove(*labels)
                except KeyError:
                    pass
            self._known_labels[gauge] = current

