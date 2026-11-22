"""Read-only Linux hardware temperature sensor discovery and session tracking."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import functools
from pathlib import Path
import re
import time
from typing import Optional

HWMON_ROOT = Path("/sys/class/hwmon")
THERMAL_ROOT = Path("/sys/class/thermal")
CPUINFO_PATH = Path("/proc/cpuinfo")

MAX_VIRTUAL_ID = "__max__"
HISTORY_LIMIT = 120

TEMP_WARM_THRESHOLD_C = 65.0
TEMP_HOT_THRESHOLD_C = 80.0

# Mapping from kernel driver name to (friendly group name, sort priority).
DRIVER_METADATA: dict[str, tuple[str, int]] = {
    "k10temp": ("CPU", 0),
    "coretemp": ("CPU", 0),
    "zenpower": ("CPU", 0),
    "cpu_thermal": ("CPU", 0),
    "x86_pkg_temp": ("CPU", 0),
    "amdgpu": ("GPU", 1),
    "radeon": ("GPU", 1),
    "nouveau": ("GPU", 1),
    "i915": ("GPU", 1),
    "xe": ("GPU", 1),
    "nvme": ("SSD NVMe", 2),
    "acpitz": ("Placa-mãe", 3),
}

_TEMP_INPUT_RE = re.compile(r"^temp(\d+)_input$")


@dataclass(frozen=True)
class SensorReading:
    """Snapshot of a single hardware temperature sensor."""

    sensor_id: str
    driver: str
    label: str
    display_name: str
    temp_c: float
    crit_c: Optional[float] = None
    max_c: Optional[float] = None
    priority: int = 4
    device_model: Optional[str] = None


@functools.lru_cache(maxsize=1)
def read_cpu_model() -> str:
    """Return the CPU model string from /proc/cpuinfo, or empty string if unavailable."""
    try:
        with CPUINFO_PATH.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("model name"):
                    _, _, value = line.partition(":")
                    return value.strip()
    except OSError:
        pass
    return ""


def _read_text(path: Path) -> Optional[str]:
    """Read and strip a sysfs text file, returning None on failure."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        return content or None
    except OSError:
        return None


def _read_millidegrees(path: Path) -> Optional[float]:
    """Read a sysfs temperature file in millidegrees Celsius and convert to °C."""
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return int(raw) / 1000.0
    except ValueError:
        return None


def _friendly_driver_info(driver: str) -> tuple[str, int]:
    """Return (group_label, priority) for a kernel hwmon driver."""
    return DRIVER_METADATA.get(driver.lower(), (driver, 4))


def _hwmon_channel_number(path: Path) -> int:
    """Extract numeric channel index from tempN_input for deterministic sorting."""
    match = _TEMP_INPUT_RE.match(path.name)
    return int(match.group(1)) if match else 0


def _read_hwmon_sensors() -> list[SensorReading]:
    """Discover and read all temperature channels under /sys/class/hwmon."""
    if not HWMON_ROOT.is_dir():
        return []

    readings: list[SensorReading] = []
    seen_ids: set[str] = set()

    for hwmon_dir in sorted(HWMON_ROOT.iterdir(), key=lambda p: p.name):
        driver = _read_text(hwmon_dir / "name") or hwmon_dir.name
        group_name, priority = _friendly_driver_info(driver)
        device_model = _read_text(hwmon_dir / "device" / "model")

        input_files = sorted(
            (p for p in hwmon_dir.iterdir() if _TEMP_INPUT_RE.match(p.name)),
            key=_hwmon_channel_number,
        )
        for input_path in input_files:
            temp_c = _read_millidegrees(input_path)
            if temp_c is None:
                continue

            channel = _hwmon_channel_number(input_path)
            prefix = f"temp{channel}"
            label = _read_text(hwmon_dir / f"{prefix}_label") or prefix
            crit_c = _read_millidegrees(hwmon_dir / f"{prefix}_crit")
            max_c = _read_millidegrees(hwmon_dir / f"{prefix}_max")

            base_id = f"{driver}:{label}"
            sensor_id = base_id if base_id not in seen_ids else f"{base_id}#{channel}"
            seen_ids.add(sensor_id)

            display_name = f"{group_name} ({label})"
            readings.append(
                SensorReading(
                    sensor_id=sensor_id,
                    driver=driver,
                    label=label,
                    display_name=display_name,
                    temp_c=temp_c,
                    crit_c=crit_c,
                    max_c=max_c,
                    priority=priority,
                    device_model=device_model,
                )
            )

    return readings


def _read_thermal_zone_sensors() -> list[SensorReading]:
    """Fallback sensor discovery using /sys/class/thermal/thermal_zone*."""
    if not THERMAL_ROOT.is_dir():
        return []

    readings: list[SensorReading] = []
    seen_ids: set[str] = set()

    for zone_dir in sorted(THERMAL_ROOT.glob("thermal_zone*"), key=lambda p: p.name):
        temp_c = _read_millidegrees(zone_dir / "temp")
        if temp_c is None:
            continue

        zone_type = _read_text(zone_dir / "type") or zone_dir.name
        group_name, priority = _friendly_driver_info(zone_type)
        base_id = f"thermal:{zone_type}"
        sensor_id = base_id if base_id not in seen_ids else f"{base_id}#{zone_dir.name}"
        seen_ids.add(sensor_id)

        readings.append(
            SensorReading(
                sensor_id=sensor_id,
                driver="thermal_zone",
                label=zone_type,
                display_name=f"{group_name} ({zone_dir.name})",
                temp_c=temp_c,
                priority=priority,
            )
        )

    return readings


def discover_and_read_sensors() -> list[SensorReading]:
    """Read current temperatures from hwmon (with thermal_zone fallback), sorted by priority."""
    readings = _read_hwmon_sensors()
    if not readings:
        readings = _read_thermal_zone_sensors()
    readings.sort(key=lambda r: (r.priority, r.sensor_id))
    return readings


def convert_temp(temp_c: float, unit: str = "C") -> float:
    """Convert Celsius to the requested display unit ('C' or 'F')."""
    if unit.upper() == "F":
        return (temp_c * 9.0 / 5.0) + 32.0
    return temp_c


def format_temp(temp_c: float, unit: str = "C") -> str:
    """Format a temperature rounded to the nearest integer with degree symbol."""
    unit_norm = "F" if unit.upper() == "F" else "C"
    value = round(convert_temp(temp_c, unit_norm))
    return f"{value}°{unit_norm}"


def format_temp_precise(temp_c: float, unit: str = "C") -> str:
    """Format a temperature with one decimal place."""
    unit_norm = "F" if unit.upper() == "F" else "C"
    value = convert_temp(temp_c, unit_norm)
    return f"{value:.1f}°{unit_norm}"


def icon_name_for_temp(temp_c: float) -> str:
    """Return the themed icon name corresponding to the temperature threshold."""
    if temp_c >= TEMP_HOT_THRESHOLD_C:
        return "temprl-hot"
    if temp_c >= TEMP_WARM_THRESHOLD_C:
        return "temprl-warm"
    return "temprl-cool"


class SensorTracker:
    """Polls hardware sensors and maintains session highs, lows, and recent history."""

    def __init__(self, history_limit: int = HISTORY_LIMIT) -> None:
        self._history_limit = history_limit
        self._history: dict[str, deque[tuple[float, float]]] = {}
        self._highest: dict[str, float] = {}
        self._lowest: dict[str, float] = {}
        self._readings: dict[str, SensorReading] = {}
        self._ordered_ids: list[str] = []

    @property
    def ordered_ids(self) -> list[str]:
        """Return detected physical sensor IDs in display order."""
        return list(self._ordered_ids)

    def _record_sample(self, sensor_id: str, now: float, temp_c: float) -> None:
        """Append a sample and update session highest/lowest bounds."""
        if sensor_id not in self._history:
            self._history[sensor_id] = deque(maxlen=self._history_limit)
        self._history[sensor_id].append((now, temp_c))

        prev_high = self._highest.get(sensor_id)
        self._highest[sensor_id] = temp_c if prev_high is None else max(prev_high, temp_c)

        prev_low = self._lowest.get(sensor_id)
        self._lowest[sensor_id] = temp_c if prev_low is None else min(prev_low, temp_c)

    def poll(self) -> list[SensorReading]:
        """Poll all sensors once and update tracked history and virtual max reading."""
        now = time.monotonic()
        readings = discover_and_read_sensors()

        self._readings.clear()
        self._ordered_ids = []
        for reading in readings:
            self._readings[reading.sensor_id] = reading
            self._ordered_ids.append(reading.sensor_id)
            self._record_sample(reading.sensor_id, now, reading.temp_c)

        if readings:
            hottest = max(readings, key=lambda r: r.temp_c)
            self._readings[MAX_VIRTUAL_ID] = SensorReading(
                sensor_id=MAX_VIRTUAL_ID,
                driver="virtual",
                label=hottest.display_name,
                display_name=f"Maior temperatura ({hottest.display_name})",
                temp_c=hottest.temp_c,
                crit_c=hottest.crit_c,
                max_c=hottest.max_c,
                priority=-1,
            )
            self._record_sample(MAX_VIRTUAL_ID, now, hottest.temp_c)

        return readings

    def get_default_sensor_id(self) -> str:
        """Select the primary CPU sensor if present, else the first available sensor."""
        for sensor_id in self._ordered_ids:
            reading = self._readings.get(sensor_id)
            if reading and reading.priority == 0:
                return sensor_id
        if self._ordered_ids:
            return self._ordered_ids[0]
        return MAX_VIRTUAL_ID

    def get_reading(self, sensor_id: str) -> Optional[SensorReading]:
        """Return the latest reading for sensor_id (physical or virtual)."""
        return self._readings.get(sensor_id)

    def get_highest(self, sensor_id: str) -> Optional[float]:
        """Return the session maximum in °C for sensor_id."""
        return self._highest.get(sensor_id)

    def get_highest_window(
        self,
        sensor_id: str,
        window_seconds: float = 60.0,
    ) -> Optional[float]:
        """Return the maximum °C for sensor_id within the last window_seconds."""
        cutoff = time.monotonic() - window_seconds
        samples = self._history.get(sensor_id, ())
        recent = [temp_c for ts, temp_c in samples if ts >= cutoff]
        if recent:
            return max(recent)
        reading = self._readings.get(sensor_id)
        return reading.temp_c if reading is not None else None

    def get_highest_for_mode(
        self,
        sensor_id: str,
        mode: str = "session",
    ) -> Optional[float]:
        """Return session maximum or rolling 60s maximum according to mode."""
        if mode == "60s":
            return self.get_highest_window(sensor_id, 60.0)
        return self.get_highest(sensor_id)

    def get_lowest(self, sensor_id: str) -> Optional[float]:
        """Return the session minimum in °C for sensor_id."""
        return self._lowest.get(sensor_id)

    def get_history(self, sensor_id: str) -> list[tuple[float, float]]:
        """Return recent (monotonic_timestamp, temp_c) samples for sensor_id."""
        samples = self._history.get(sensor_id)
        return list(samples) if samples else []

    def reset_highest(self) -> None:
        """Reset session highs and lows to the current temperature of each active sensor."""
        self._highest.clear()
        self._lowest.clear()
        for sensor_id, reading in self._readings.items():
            self._highest[sensor_id] = reading.temp_c
            self._lowest[sensor_id] = reading.temp_c
