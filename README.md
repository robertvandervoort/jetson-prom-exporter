# Jetson Prometheus Exporter

Prometheus exporter for NVIDIA Jetson devices using the `jetson-stats` / `jtop`
Python API for structured hardware metrics. A small supplemental `nvidia-smi
dmon` poller remains for GPU dmon counters that jtop does not expose directly.

> Last updated: 2026-06-25 13:47:24 CDT

## Metrics

| Prefix | Source | Examples |
|---|---|---|
| `jetson_jtop_board_info` | jtop | model, JetPack, L4T, CUDA/library labels |
| `jetson_jtop_library_info` | jtop | CUDA, cuDNN, TensorRT, OpenCV, VPI, Vulkan versions |
| `jetson_jtop_cpu_*` | jtop | per-core online state, active/user/system/idle %, frequency |
| `jetson_jtop_memory_*` | jtop | RAM, swap, IRAM, EMC usage/frequency |
| `jetson_jtop_gpu_*` | jtop | GPU load, frequency, GPC frequency, status flags |
| `jetson_jtop_engine_*` | jtop | DLA/NVENC/NVDEC/NVJPG/OFA/VIC online state and frequency |
| `jetson_jtop_temperature_celsius` | jtop | thermal zone temperatures and limits |
| `jetson_jtop_power_*` | jtop | total and per-rail watts, volts, amps, limits |
| `jetson_jtop_fan_*` | jtop | fan PWM speed, RPM, profile/governor/control info |
| `jetson_jtop_process_*` | jtop | GPU process CPU, resident memory, GPU memory, metadata |
| `jetson_jtop_disk_*` | jtop | disk capacity/used/available bytes |
| `jetson_jtop_local_interface_info` | jtop | hostname, interface, IP address labels |
| `jetson_nvidia_smi_gpu_*` | nvidia-smi | GPU identity, utilization, memory utilization, temp, power |
| `jetson_nvidia_smi_process_gpu_memory_bytes` | nvidia-smi | per-process GPU memory when jtop reports 0 |
| `jetson_nvidia_smi_dmon_*` | nvidia-smi dmon | SM/mem/encoder/decoder/JPG/OFA %, ECC, PCIe replay errors |

The exporter intentionally uses jtop-native `jetson_jtop_*` names instead of
the old tegrastats-era metric contract. On current Thor hosts, jtop 4.3.2 may
return an empty `gpu` object even though `nvidia-smi` sees the GPU; in that case
the `jetson_nvidia_smi_gpu_*` and `jetson_nvidia_smi_process_*` metrics provide
the dashboard fallback for GPU load, temperature, power, and process memory.
Keep an eye on upstream `jetson-stats` / `jtop` Thor GPU support; once its
NVML/Thor path is stable and packaged, this fallback can be revisited.

## Usage

On a Jetson host, install the host-side jtop prerequisites first:

```bash
./install.sh
```

The script installs:

- `jetson-stats` / `jtop` into the host Python environment.
- `python3-prometheus-client` for host-side smoke tests.
- `jtop.service`, enabled and started through systemd.
- The invoking user in the `jtop` group when applicable.

If your user was newly added to the `jtop` group, log out and back in before
running jtop clients directly as that user. Containers that run as root can use
the mounted socket immediately.

Then start or rebuild the exporter:

```bash
docker compose -f docker-compose.example.yml up -d
```

In the full obs-stack deployment:

```bash
cd /opt/obs-stack
docker compose up -d --build jetson-exporter
```

The Docker image installs only runtime dependencies from PyPI and runs the local
source tree via `PYTHONPATH=/app/src`. This avoids `pip install .` build
isolation, which can fail on Jetson hosts when PyPI downloads for setuptools or
wheel time out.

Requires:

- `jetson-stats` installed on the Jetson host.
- `jtop.service` running and exposing `/run/jtop.sock`.
- `/run/jtop.sock` bind-mounted into the exporter container.
- `runtime: nvidia` for the supplemental `nvidia-smi dmon` collector.
- `pid: host` for host process visibility.

Metrics are served at `http://localhost:9101/metrics`.

Quick smoke check:

```bash
curl -fsS http://localhost:9101/metrics | grep -E 'jetson_jtop_up|jetson_jtop_cpu_utilization_percent' | head
```

## Grafana

A starter dashboard is included at:

```text
grafana/jetson-thor-hardware.json
```

It is organized into sections for overview, platform/runtime, CPU and process
activity, GPU and accelerator engines, memory/swap/EMC, disk/network, and
power/fans/thermal limits. It expects a Prometheus datasource with UID
`prometheus`.

## Development

```bash
pip install -e ".[dev]"
pytest
```
