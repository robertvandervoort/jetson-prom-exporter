"""Background threads that feed jtop and nvidia-smi data to Prometheus gauges."""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Any

from prometheus_client import Counter, Gauge, Info

from .jtop_mapper import (
    as_mapping,
    as_number,
    bool_to_float,
    capture_jtop_payload,
    flatten_board_info,
    gb_to_bytes,
    khz_to_hz,
    kb_to_bytes,
    ma_to_amps,
    mv_to_volts,
    mw_to_watts,
)
from .nvidia_smi_parser import parse_dmon, parse_query_apps, parse_query_gpu

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prometheus metric definitions
# ---------------------------------------------------------------------------

# --- jtop metrics ---
JTOP_UP = Gauge("jetson_jtop_up", "Whether the jtop client is connected and producing samples")
JTOP_SCRAPE_ERRORS = Counter("jetson_jtop_scrape_errors_total", "jtop collection failures")
JTOP_BOARD_INFO = Info("jetson_jtop_board", "Jetson platform, hardware, JetPack, L4T, and library info")
JTOP_LIBRARY_INFO = Gauge("jetson_jtop_library_info", "Installed NVIDIA/library version info", ["library", "version"])
JTOP_NVP_MODEL_INFO = Gauge("jetson_jtop_nvpmodel_info", "Active NVP power model", ["model"])
JTOP_JETSON_CLOCKS_ENABLED = Gauge("jetson_jtop_jetson_clocks_enabled", "jetson_clocks enabled state")
JTOP_JETSON_CLOCKS_BOOT_ENABLED = Gauge("jetson_jtop_jetson_clocks_boot_enabled", "jetson_clocks boot state")
JTOP_UPTIME_SECONDS = Gauge("jetson_jtop_uptime_seconds", "System uptime from jtop")

JTOP_CPU_ONLINE = Gauge("jetson_jtop_cpu_online", "CPU core online state", ["cpu"])
JTOP_CPU_UTIL = Gauge("jetson_jtop_cpu_utilization_percent", "CPU utilization by mode", ["cpu", "mode"])
JTOP_CPU_FREQ = Gauge("jetson_jtop_cpu_frequency_hertz", "CPU frequency", ["cpu", "state"])
JTOP_CPU_INFO = Gauge("jetson_jtop_cpu_info", "CPU model/governor info", ["cpu", "model", "governor"])

JTOP_MEMORY_BYTES = Gauge("jetson_jtop_memory_bytes", "Memory counters from jtop", ["memory", "state"])
JTOP_EMC_UTIL = Gauge("jetson_jtop_emc_utilization_percent", "EMC memory controller utilization")
JTOP_EMC_FREQ = Gauge("jetson_jtop_emc_frequency_hertz", "EMC memory controller frequency", ["state"])

JTOP_GPU_LOAD = Gauge("jetson_jtop_gpu_load_percent", "GPU load", ["gpu"])
JTOP_GPU_FREQ = Gauge("jetson_jtop_gpu_frequency_hertz", "GPU frequency", ["gpu", "state"])
JTOP_GPU_GPC_FREQ = Gauge("jetson_jtop_gpu_gpc_frequency_hertz", "GPU GPC frequency", ["gpu", "gpc"])
JTOP_GPU_STATUS = Gauge("jetson_jtop_gpu_status", "GPU status booleans", ["gpu", "field"])

JTOP_ENGINE_ONLINE = Gauge("jetson_jtop_engine_online", "Hardware engine online state", ["group", "engine"])
JTOP_ENGINE_FREQ = Gauge("jetson_jtop_engine_frequency_hertz", "Hardware engine frequency", ["group", "engine", "state"])

JTOP_TEMPERATURE = Gauge("jetson_jtop_temperature_celsius", "Thermal zone temperatures and limits", ["zone", "state"])
JTOP_POWER_WATTS = Gauge("jetson_jtop_power_watts", "Power rail readings", ["rail", "state"])
JTOP_POWER_VOLTS = Gauge("jetson_jtop_power_volts", "Power rail voltage", ["rail"])
JTOP_POWER_AMPS = Gauge("jetson_jtop_power_amps", "Power rail current", ["rail"])
JTOP_FAN_SPEED = Gauge("jetson_jtop_fan_speed_percent", "Fan PWM speed", ["fan", "index"])
JTOP_FAN_RPM = Gauge("jetson_jtop_fan_rpm", "Fan RPM", ["fan", "index"])
JTOP_FAN_INFO = Gauge("jetson_jtop_fan_info", "Fan profile/governor/control info", ["fan", "profile", "governor", "control"])

JTOP_DISK_BYTES = Gauge("jetson_jtop_disk_bytes", "Disk usage from jtop", ["state"])
JTOP_PROCESS_INFO = Gauge(
    "jetson_jtop_process_info",
    "GPU process metadata from jtop",
    ["pid", "user", "gpu", "type", "state", "process_name"],
)
JTOP_PROCESS_CPU = Gauge("jetson_jtop_process_cpu_utilization_percent", "GPU process CPU utilization", ["pid", "process_name"])
JTOP_PROCESS_MEMORY_BYTES = Gauge("jetson_jtop_process_memory_bytes", "GPU process resident memory", ["pid", "process_name"])
JTOP_PROCESS_GPU_MEMORY_BYTES = Gauge("jetson_jtop_process_gpu_memory_bytes", "GPU process memory", ["pid", "process_name"])
JTOP_LOCAL_INTERFACE_INFO = Gauge(
    "jetson_jtop_local_interface_info",
    "Local network interface addresses from jtop",
    ["hostname", "interface", "address"],
)

# --- supplemental nvidia-smi dmon metrics ---
NVIDIA_SMI_GPU_INFO = Info("jetson_nvidia_smi_gpu", "GPU info labels from nvidia-smi")
NVIDIA_SMI_GPU_UTIL = Gauge("jetson_nvidia_smi_gpu_utilization_percent", "GPU utilization from nvidia-smi")
NVIDIA_SMI_GPU_MEM_UTIL = Gauge("jetson_nvidia_smi_gpu_memory_utilization_percent", "GPU memory utilization from nvidia-smi")
NVIDIA_SMI_GPU_TEMP = Gauge("jetson_nvidia_smi_gpu_temperature_celsius", "GPU temperature from nvidia-smi")
NVIDIA_SMI_GPU_POWER = Gauge("jetson_nvidia_smi_gpu_power_draw_watts", "GPU power draw from nvidia-smi")
NVIDIA_SMI_PROCESS_GPU_MEMORY = Gauge(
    "jetson_nvidia_smi_process_gpu_memory_bytes",
    "Per-process GPU memory from nvidia-smi",
    ["pid", "process_name"],
)
NVIDIA_SMI_DMON_UTIL = Gauge("jetson_nvidia_smi_dmon_utilization_percent", "dmon utilization counters", ["unit"])
NVIDIA_SMI_DMON_ECC = Gauge("jetson_nvidia_smi_dmon_ecc_errors", "dmon ECC error counters", ["type"])
NVIDIA_SMI_DMON_PCIE = Gauge("jetson_nvidia_smi_dmon_pcie_replay_errors", "dmon PCIe replay errors")

_QUERY_GPU_FIELDS = "name,driver_version,temperature.gpu,power.draw,utilization.gpu,utilization.memory,compute_mode"
_QUERY_APP_FIELDS = "pid,process_name,used_gpu_memory"


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


class LabelTracker:
    """Remove stale labelled time series when jtop sections disappear."""

    def __init__(self) -> None:
        self._seen: dict[Gauge, set[tuple[str, ...]]] = {}

    def set(self, gauge: Gauge, label_values: tuple[str, ...], value: float) -> None:
        gauge.labels(*label_values).set(value)
        self._seen.setdefault(gauge, set()).add(label_values)

    def reconcile(self, observed: dict[Gauge, set[tuple[str, ...]]]) -> None:
        for gauge, previous in list(self._seen.items()):
            current = observed.get(gauge, set())
            for labels in previous - current:
                try:
                    gauge.remove(*labels)
                except KeyError:
                    pass
            self._seen[gauge] = current


# ---------------------------------------------------------------------------
# jtop collector thread
# ---------------------------------------------------------------------------


class JtopCollector(threading.Thread):
    """Reads structured Jetson metrics from the jtop Python API."""

    def __init__(self, interval_ms: int = 30000, jtop_factory: Any | None = None):
        super().__init__(daemon=True, name="jtop-collector")
        self.interval_sec = interval_ms / 1000.0
        self._jtop_factory = jtop_factory
        self._labels = LabelTracker()

    def run(self) -> None:
        while True:
            try:
                self._stream()
            except Exception:
                JTOP_UP.set(0)
                JTOP_SCRAPE_ERRORS.inc()
                log.exception("jtop collection failed, reconnecting in 5s")
                time.sleep(5)

    def _load_jtop_factory(self) -> Any:
        if self._jtop_factory is not None:
            return self._jtop_factory
        from jtop import jtop  # type: ignore[import-not-found]

        return jtop

    def _stream(self) -> None:
        factory = self._load_jtop_factory()
        log.info("Starting jtop client interval=%.3fs", self.interval_sec)
        with factory(interval=self.interval_sec) as jetson:
            while jetson.ok():
                JTOP_UP.set(1)
                self.update_from_payload(capture_jtop_payload(jetson), self._labels)
        JTOP_UP.set(0)

    @classmethod
    def update_from_payload(cls, payload: dict[str, Any], labels: LabelTracker | None = None) -> None:
        tracker = labels or LabelTracker()
        observed: dict[Gauge, set[tuple[str, ...]]] = {}

        def set_labelled(gauge: Gauge, label_values: tuple[str, ...], value: float | None) -> None:
            if value is None:
                return
            tracker.set(gauge, label_values, value)
            observed.setdefault(gauge, set()).add(label_values)

        cls._update_board(payload, set_labelled)
        cls._update_cpu(payload, set_labelled)
        cls._update_memory(payload, set_labelled)
        cls._update_gpu(payload, set_labelled)
        cls._update_engines(payload, set_labelled)
        cls._update_temperature(payload, set_labelled)
        cls._update_power(payload, set_labelled)
        cls._update_fans(payload, set_labelled)
        cls._update_disk(payload, set_labelled)
        cls._update_processes(payload, set_labelled)
        cls._update_local_interfaces(payload, set_labelled)
        tracker.reconcile(observed)

    @staticmethod
    def _update_board(payload: dict[str, Any], set_labelled: Any) -> None:
        board = as_mapping(payload.get("board"))
        info = flatten_board_info(board)
        if info:
            JTOP_BOARD_INFO.info(info)
        for library, version in as_mapping(board.get("libraries")).items():
            if version is not None:
                set_labelled(JTOP_LIBRARY_INFO, (str(library), str(version)), 1.0)

        uptime = as_number(payload.get("uptime"))
        if uptime is not None:
            JTOP_UPTIME_SECONDS.set(uptime)

        nvpmodel = payload.get("nvpmodel")
        if nvpmodel is not None:
            set_labelled(JTOP_NVP_MODEL_INFO, (str(nvpmodel),), 1.0)

        clocks = payload.get("jetson_clocks")
        clock_map = as_mapping(clocks)
        if clock_map:
            _set_if(JTOP_JETSON_CLOCKS_ENABLED, bool_to_float(clock_map.get("status")))
            _set_if(JTOP_JETSON_CLOCKS_BOOT_ENABLED, bool_to_float(clock_map.get("boot")))
        else:
            _set_if(JTOP_JETSON_CLOCKS_ENABLED, bool_to_float(clocks))

    @staticmethod
    def _update_cpu(payload: dict[str, Any], set_labelled: Any) -> None:
        cpu = as_mapping(payload.get("cpu"))
        cores = cpu.get("cpu", [])
        if not isinstance(cores, list):
            return
        for index, core_payload in enumerate(cores):
            core = as_mapping(core_payload)
            cpu_label = str(index)
            set_labelled(JTOP_CPU_ONLINE, (cpu_label,), bool_to_float(core.get("online")))
            for mode in ("user", "nice", "system", "idle"):
                set_labelled(JTOP_CPU_UTIL, (cpu_label, mode), as_number(core.get(mode)))
            idle = as_number(core.get("idle"))
            if idle is not None:
                set_labelled(JTOP_CPU_UTIL, (cpu_label, "active"), max(0.0, 100.0 - idle))
            for state, value in as_mapping(core.get("freq")).items():
                set_labelled(JTOP_CPU_FREQ, (cpu_label, str(state)), khz_to_hz(value))
            if core.get("model") or core.get("governor"):
                set_labelled(
                    JTOP_CPU_INFO,
                    (cpu_label, str(core.get("model", "unknown")), str(core.get("governor", "unknown"))),
                    1.0,
                )

    @staticmethod
    def _update_memory(payload: dict[str, Any], set_labelled: Any) -> None:
        memory = as_mapping(payload.get("memory"))
        for name in ("RAM", "SWAP", "IRAM"):
            section = as_mapping(memory.get(name))
            for state, value in section.items():
                if state == "table":
                    continue
                set_labelled(JTOP_MEMORY_BYTES, (name.lower(), str(state)), kb_to_bytes(value))

        emc = as_mapping(memory.get("EMC"))
        _set_if(JTOP_EMC_UTIL, as_number(emc.get("val")))
        for state in ("cur", "min", "max"):
            set_labelled(JTOP_EMC_FREQ, (state,), khz_to_hz(emc.get(state)))

    @staticmethod
    def _update_gpu(payload: dict[str, Any], set_labelled: Any) -> None:
        gpu_payload = as_mapping(payload.get("gpu"))
        for name, value in gpu_payload.items():
            gpu = as_mapping(value)
            gpu_name = str(name)
            status = as_mapping(gpu.get("status"))
            set_labelled(JTOP_GPU_LOAD, (gpu_name,), as_number(status.get("load")))
            for field in ("railgate", "tpc_pg_mask", "3d_scaling"):
                set_labelled(JTOP_GPU_STATUS, (gpu_name, field), bool_to_float(status.get(field)))
            freq = as_mapping(gpu.get("freq"))
            for state in ("cur", "min", "max"):
                set_labelled(JTOP_GPU_FREQ, (gpu_name, state), khz_to_hz(freq.get(state)))
            gpcs = freq.get("GPC")
            if isinstance(gpcs, list):
                for index, gpc_freq in enumerate(gpcs):
                    set_labelled(JTOP_GPU_GPC_FREQ, (gpu_name, str(index)), khz_to_hz(gpc_freq))

    @staticmethod
    def _update_engines(payload: dict[str, Any], set_labelled: Any) -> None:
        engines = as_mapping(payload.get("engine"))
        for group_name, group_payload in engines.items():
            group = as_mapping(group_payload)
            for engine_name, engine_payload in group.items():
                engine = as_mapping(engine_payload)
                labels = (str(group_name), str(engine_name))
                set_labelled(JTOP_ENGINE_ONLINE, labels, bool_to_float(engine.get("online")))
                for state in ("cur", "min", "max"):
                    set_labelled(JTOP_ENGINE_FREQ, labels + (state,), khz_to_hz(engine.get(state)))

    @staticmethod
    def _update_temperature(payload: dict[str, Any], set_labelled: Any) -> None:
        temperatures = as_mapping(payload.get("temperature"))
        for zone_name, zone_payload in temperatures.items():
            zone = as_mapping(zone_payload)
            for state, value in zone.items():
                if state == "online":
                    continue
                set_labelled(JTOP_TEMPERATURE, (str(zone_name), str(state)), as_number(value))

    @staticmethod
    def _update_power(payload: dict[str, Any], set_labelled: Any) -> None:
        power = as_mapping(payload.get("power"))
        total = as_mapping(power.get("tot"))
        if total:
            set_labelled(JTOP_POWER_WATTS, ("total", "current"), mw_to_watts(total.get("power")))
            set_labelled(JTOP_POWER_WATTS, ("total", "average"), mw_to_watts(total.get("avg")))
        for rail_name, rail_payload in as_mapping(power.get("rail")).items():
            rail = as_mapping(rail_payload)
            rail_label = str(rail_name)
            set_labelled(JTOP_POWER_WATTS, (rail_label, "current"), mw_to_watts(rail.get("power")))
            set_labelled(JTOP_POWER_WATTS, (rail_label, "average"), mw_to_watts(rail.get("avg")))
            set_labelled(JTOP_POWER_WATTS, (rail_label, "warning_limit"), mw_to_watts(rail.get("warn")))
            set_labelled(JTOP_POWER_WATTS, (rail_label, "critical_limit"), mw_to_watts(rail.get("crit")))
            set_labelled(JTOP_POWER_VOLTS, (rail_label,), mv_to_volts(rail.get("volt")))
            set_labelled(JTOP_POWER_AMPS, (rail_label,), ma_to_amps(rail.get("curr")))

    @staticmethod
    def _update_fans(payload: dict[str, Any], set_labelled: Any) -> None:
        fans = as_mapping(payload.get("fan"))
        for fan_name, fan_payload in fans.items():
            fan = as_mapping(fan_payload)
            fan_label = str(fan_name)
            for field, gauge in (("speed", JTOP_FAN_SPEED), ("rpm", JTOP_FAN_RPM)):
                values = fan.get(field, [])
                if not isinstance(values, list):
                    values = [values]
                for index, value in enumerate(values):
                    set_labelled(gauge, (fan_label, str(index)), as_number(value))
            set_labelled(
                JTOP_FAN_INFO,
                (
                    fan_label,
                    str(fan.get("profile", "unknown")),
                    str(fan.get("governor", "unknown")),
                    str(fan.get("control", "unknown")),
                ),
                1.0,
            )

    @staticmethod
    def _update_disk(payload: dict[str, Any], set_labelled: Any) -> None:
        disk = as_mapping(payload.get("disk"))
        for state in ("total", "used", "available", "available_no_root"):
            set_labelled(JTOP_DISK_BYTES, (state,), gb_to_bytes(disk.get(state)))

    @staticmethod
    def _update_processes(payload: dict[str, Any], set_labelled: Any) -> None:
        processes = payload.get("processes", [])
        if not isinstance(processes, list):
            return
        for row in processes:
            if not isinstance(row, list) or len(row) < 10:
                continue
            pid, user, gpu, proc_type, _priority, state, cpu_pct, mem, gpu_mem, name = row[:10]
            pid_label = str(pid)
            name_label = str(name)
            set_labelled(
                JTOP_PROCESS_INFO,
                (pid_label, str(user), str(gpu), str(proc_type), str(state), name_label),
                1.0,
            )
            set_labelled(JTOP_PROCESS_CPU, (pid_label, name_label), as_number(cpu_pct))
            set_labelled(JTOP_PROCESS_MEMORY_BYTES, (pid_label, name_label), kb_to_bytes(mem))
            set_labelled(JTOP_PROCESS_GPU_MEMORY_BYTES, (pid_label, name_label), kb_to_bytes(gpu_mem))

    @staticmethod
    def _update_local_interfaces(payload: dict[str, Any], set_labelled: Any) -> None:
        local = as_mapping(payload.get("local_interfaces"))
        hostname = str(local.get("hostname", "unknown"))
        for interface, addresses in as_mapping(local.get("interfaces")).items():
            if isinstance(addresses, str):
                addresses = [addresses]
            if not isinstance(addresses, list):
                continue
            for address in addresses:
                set_labelled(JTOP_LOCAL_INTERFACE_INFO, (hostname, str(interface), str(address)), 1.0)


# ---------------------------------------------------------------------------
# supplemental nvidia-smi dmon collector thread
# ---------------------------------------------------------------------------


class NvidiaSmiCollector(threading.Thread):
    """Polls nvidia-smi for GPU counters not exposed reliably by jtop on Thor."""

    def __init__(self, interval_sec: float = 5.0, nvidia_smi_path: str = "/usr/bin/nvidia-smi"):
        super().__init__(daemon=True, name="nvidia-smi-collector")
        self.interval_sec = interval_sec
        self.nvidia_smi_path = nvidia_smi_path
        self._info_set = False
        self._known_procs: dict[str, str] = {}

    def run(self) -> None:
        while True:
            try:
                self._poll()
            except Exception:
                log.exception("nvidia-smi dmon poll failed")
            time.sleep(self.interval_sec)

    def _run_cmd(self, args: list[str]) -> str | None:
        try:
            r = subprocess.run(
                [self.nvidia_smi_path] + args,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode != 0:
                log.warning("nvidia-smi %s returned %d: %s", args[0], r.returncode, r.stderr[:200])
                return None
            return r.stdout
        except FileNotFoundError:
            log.warning("nvidia-smi not found at %s", self.nvidia_smi_path)
            return None
        except subprocess.TimeoutExpired:
            log.warning("nvidia-smi timed out")
            return None

    def _poll(self) -> None:
        out = self._run_cmd(["--query-gpu=" + _QUERY_GPU_FIELDS, "--format=csv"])
        if out:
            data = parse_query_gpu(out)
            _set_if(NVIDIA_SMI_GPU_TEMP, data.get("temperature_gpu"))
            _set_if(NVIDIA_SMI_GPU_POWER, data.get("power_draw"))
            _set_if(NVIDIA_SMI_GPU_UTIL, data.get("utilization_gpu"))
            _set_if(NVIDIA_SMI_GPU_MEM_UTIL, data.get("utilization_memory"))

            if not self._info_set:
                info_vals = {}
                for key in ("name", "driver_version", "compute_mode"):
                    val = data.get(key)
                    if val is not None:
                        info_vals[key] = str(val)
                if info_vals:
                    NVIDIA_SMI_GPU_INFO.info(info_vals)
                    self._info_set = True

        out = self._run_cmd(["--query-compute-apps=" + _QUERY_APP_FIELDS, "--format=csv"])
        if out:
            apps = parse_query_apps(out)
            current_procs: dict[str, str] = {}
            for app in apps:
                pid = str(app.get("pid", ""))
                process_name = str(app.get("process_name", ""))
                mem_mib = _safe_float(app.get("used_gpu_memory"))
                if pid and mem_mib is not None:
                    NVIDIA_SMI_PROCESS_GPU_MEMORY.labels(pid=pid, process_name=process_name).set(mem_mib * 1024 * 1024)
                    current_procs[pid] = process_name
            for pid in set(self._known_procs) - set(current_procs):
                try:
                    NVIDIA_SMI_PROCESS_GPU_MEMORY.remove(pid, self._known_procs[pid])
                except KeyError:
                    pass
            self._known_procs = current_procs

        out = self._run_cmd(["dmon", "-s", "pucem", "-c", "1"])
        if not out:
            return
        dm = parse_dmon(out)
        for unit in ("sm", "mem", "enc", "dec", "jpg", "ofa"):
            val = _safe_float(dm.get(unit))
            if val is not None:
                NVIDIA_SMI_DMON_UTIL.labels(unit=unit).set(val)
        _set_label_if(NVIDIA_SMI_DMON_ECC, ("singlebit",), dm.get("sbecc"))
        _set_label_if(NVIDIA_SMI_DMON_ECC, ("doublebit",), dm.get("dbecc"))
        _set_if(NVIDIA_SMI_DMON_PCIE, dm.get("pci"))


def _set_if(gauge: Gauge, val: Any) -> None:
    v = _safe_float(val)
    if v is not None:
        gauge.set(v)


def _set_label_if(gauge: Gauge, label_values: tuple[str, ...], val: Any) -> None:
    v = _safe_float(val)
    if v is not None:
        gauge.labels(*label_values).set(v)
