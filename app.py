#!/usr/bin/env python3
"""Self-contained Ubuntu 26.04 system tray temperature monitor (temprl)."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
from typing import Optional

import gi

gi.require_version("AyatanaAppIndicator3", "0.1")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "3.0")
from gi.repository import AyatanaAppIndicator3, GLib, Gtk  # noqa: E402

from history_window import TemperatureWindow  # noqa: E402
from sensors import (  # noqa: E402
    MAX_VIRTUAL_ID,
    SensorTracker,
    format_temp,
    icon_name_for_temp,
    read_cpu_model,
)

APP_DIR = Path(__file__).resolve().parent
ICONS_DIR = APP_DIR / "icons"
CONFIG_PATH = APP_DIR / "config.json"
LOCK_PATH = APP_DIR / ".temprl.lock"
DESKTOP_ENTRY_PATH = Path.home() / ".local" / "share" / "applications" / "temprl.desktop"

VALID_INTERVALS = (1, 2, 5)
VALID_UNITS = ("C", "F")
VALID_MAX_MODES = ("session", "60s")


class SingleInstanceLock:
    """Exclusive file lock kept inside APP_DIR to prevent duplicate tray icons."""

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._handle: Optional[object] = None

    def acquire(self) -> bool:
        """Try to acquire an exclusive non-blocking lock; return False if already running."""
        handle = self._lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False

        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        """Release the lock file handle."""
        if self._handle is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


def load_config(default_sensor_id: str) -> dict[str, object]:
    """Load user preferences from config.json inside APP_DIR with safe defaults."""
    config: dict[str, object] = {
        "sensor_id": default_sensor_id,
        "interval_sec": 2,
        "unit": "C",
        "show_max_in_tray": False,
        "max_mode": "session",
    }
    if not CONFIG_PATH.is_file():
        return config

    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return config

    if not isinstance(raw, dict):
        return config

    sensor_id = raw.get("sensor_id")
    if isinstance(sensor_id, str) and sensor_id:
        config["sensor_id"] = sensor_id

    interval_sec = raw.get("interval_sec")
    if isinstance(interval_sec, int) and interval_sec in VALID_INTERVALS:
        config["interval_sec"] = interval_sec

    unit = raw.get("unit")
    if isinstance(unit, str) and unit.upper() in VALID_UNITS:
        config["unit"] = unit.upper()

    show_max_in_tray = raw.get("show_max_in_tray")
    if isinstance(show_max_in_tray, bool):
        config["show_max_in_tray"] = show_max_in_tray

    max_mode = raw.get("max_mode")
    if isinstance(max_mode, str) and max_mode in VALID_MAX_MODES:
        config["max_mode"] = max_mode

    return config


def save_config(config: dict[str, object]) -> None:
    """Persist config atomically inside APP_DIR without touching external directories."""
    tmp_path = APP_DIR / ".config.json.tmp"
    payload = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    try:
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, CONFIG_PATH)
    except OSError:
        pass


class TempTrayApp:
    """Ubuntu top-bar system tray indicator displaying live hardware temperature."""

    def __init__(self) -> None:
        self.tracker = SensorTracker()
        self.tracker.poll()

        default_sensor = self.tracker.get_default_sensor_id()
        self.config = load_config(default_sensor)

        if self.tracker.get_reading(str(self.config["sensor_id"])) is None:
            self.config["sensor_id"] = default_sensor

        self._updating_menu = False
        self._last_icon_name: Optional[str] = None
        self._timeout_id: Optional[int] = None
        self._window: Optional[TemperatureWindow] = None

        self._sensor_items: dict[str, Gtk.RadioMenuItem] = {}
        self._interval_items: dict[int, Gtk.RadioMenuItem] = {}
        self._unit_items: dict[str, Gtk.RadioMenuItem] = {}
        self._max_mode_items: dict[str, Gtk.RadioMenuItem] = {}

        self.indicator = AyatanaAppIndicator3.Indicator.new(
            "temprl-indicator",
            "temprl-cool",
            AyatanaAppIndicator3.IndicatorCategory.HARDWARE,
        )
        self.indicator.set_icon_theme_path(str(ICONS_DIR))
        self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)

        self._menu = self._build_menu()
        self._menu.show_all()
        self.indicator.set_menu(self._menu)

        self._refresh_ui()
        self._schedule_timer()

        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._on_unix_signal)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._on_unix_signal)

    @property
    def selected_sensor_id(self) -> str:
        return str(self.config["sensor_id"])

    @property
    def interval_sec(self) -> int:
        return int(self.config["interval_sec"])

    @property
    def unit(self) -> str:
        return str(self.config["unit"])

    @property
    def show_max_in_tray(self) -> bool:
        return bool(self.config["show_max_in_tray"])

    @property
    def max_mode(self) -> str:
        return str(self.config["max_mode"])

    def _build_menu(self) -> Gtk.Menu:
        """Create the persistent tray dropdown menu (labels updated in place on ticks)."""
        menu = Gtk.Menu()

        self._summary_item = Gtk.MenuItem(label="Carregando sensores...")
        self._summary_item.set_sensitive(False)
        menu.append(self._summary_item)
        menu.append(Gtk.SeparatorMenuItem())

        group_anchor: Optional[Gtk.RadioMenuItem] = None
        sensor_choices = [(sid, self.tracker.get_reading(sid)) for sid in self.tracker.ordered_ids]
        if self.tracker.get_reading(MAX_VIRTUAL_ID) is not None:
            sensor_choices.append((MAX_VIRTUAL_ID, None))

        for sensor_id, reading in sensor_choices:
            if sensor_id == MAX_VIRTUAL_ID:
                label = "Maior temperatura (Automático)"
            else:
                label = reading.display_name if reading else sensor_id

            item = Gtk.RadioMenuItem.new_with_label_from_widget(group_anchor, label)
            if group_anchor is None:
                group_anchor = item
            item.set_active(sensor_id == self.selected_sensor_id)
            item.connect("toggled", self._on_sensor_toggled, sensor_id)
            menu.append(item)
            self._sensor_items[sensor_id] = item

        menu.append(Gtk.SeparatorMenuItem())

        graph_item = Gtk.MenuItem(label="Abrir gráfico em tempo real")
        graph_item.connect("activate", self._on_open_window)
        menu.append(graph_item)

        reset_item = Gtk.MenuItem(label="Zerar máxima da sessão")
        reset_item.connect("activate", self._on_reset_highest)
        menu.append(reset_item)

        self._show_max_item = Gtk.CheckMenuItem(label="Mostrar máxima na bandeja")
        self._show_max_item.set_active(self.show_max_in_tray)
        self._show_max_item.connect("toggled", self._on_show_max_toggled)
        menu.append(self._show_max_item)

        menu.append(self._build_max_mode_submenu())
        menu.append(self._build_interval_submenu())
        menu.append(self._build_unit_submenu())
        menu.append(Gtk.SeparatorMenuItem())

        remove_shortcut_item = Gtk.MenuItem(label="Remover atalho do sistema e sair")
        remove_shortcut_item.connect("activate", self._on_remove_shortcut_and_quit)
        menu.append(remove_shortcut_item)

        quit_item = Gtk.MenuItem(label="Sair")
        quit_item.connect("activate", self._on_quit_clicked)
        menu.append(quit_item)

        return menu

    def _build_max_mode_submenu(self) -> Gtk.MenuItem:
        """Build maximum calculation mode submenu (session vs rolling 60s)."""
        parent = Gtk.MenuItem(label="Modo da máxima")
        submenu = Gtk.Menu()
        anchor: Optional[Gtk.RadioMenuItem] = None
        labels = {
            "session": "Máxima da sessão",
            "60s": "Máxima dos últimos 60s",
        }

        for mode_code in VALID_MAX_MODES:
            item = Gtk.RadioMenuItem.new_with_label_from_widget(anchor, labels[mode_code])
            if anchor is None:
                anchor = item
            item.set_active(mode_code == self.max_mode)
            item.connect("toggled", self._on_max_mode_toggled, mode_code)
            submenu.append(item)
            self._max_mode_items[mode_code] = item

        parent.set_submenu(submenu)
        return parent

    def _build_interval_submenu(self) -> Gtk.MenuItem:
        """Build polling interval submenu."""
        parent = Gtk.MenuItem(label="Intervalo de atualização")
        submenu = Gtk.Menu()
        anchor: Optional[Gtk.RadioMenuItem] = None
        labels = {1: "1 segundo", 2: "2 segundos", 5: "5 segundos"}

        for seconds in VALID_INTERVALS:
            item = Gtk.RadioMenuItem.new_with_label_from_widget(anchor, labels[seconds])
            if anchor is None:
                anchor = item
            item.set_active(seconds == self.interval_sec)
            item.connect("toggled", self._on_interval_toggled, seconds)
            submenu.append(item)
            self._interval_items[seconds] = item

        parent.set_submenu(submenu)
        return parent

    def _build_unit_submenu(self) -> Gtk.MenuItem:
        """Build temperature unit submenu (°C / °F)."""
        parent = Gtk.MenuItem(label="Unidade")
        submenu = Gtk.Menu()
        anchor: Optional[Gtk.RadioMenuItem] = None
        labels = {"C": "Celsius (°C)", "F": "Fahrenheit (°F)"}

        for unit_code in VALID_UNITS:
            item = Gtk.RadioMenuItem.new_with_label_from_widget(anchor, labels[unit_code])
            if anchor is None:
                anchor = item
            item.set_active(unit_code == self.unit)
            item.connect("toggled", self._on_unit_toggled, unit_code)
            submenu.append(item)
            self._unit_items[unit_code] = item

        parent.set_submenu(submenu)
        return parent

    def _schedule_timer(self) -> None:
        """Start or restart the periodic GLib polling timer."""
        if self._timeout_id is not None:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = None
        self._timeout_id = GLib.timeout_add_seconds(self.interval_sec, self._on_tick)

    def _on_tick(self) -> bool:
        """Poll sensors and refresh indicator + visible UI."""
        self.tracker.poll()
        self._refresh_ui()
        return GLib.SOURCE_CONTINUE

    def _refresh_ui(self) -> None:
        """Update top-bar text/icon, in-place menu labels, and chart window if open."""
        reading = self.tracker.get_reading(self.selected_sensor_id)
        if reading is None:
            fallback_id = self.tracker.get_default_sensor_id()
            reading = self.tracker.get_reading(fallback_id)
            if reading is not None:
                self.config["sensor_id"] = fallback_id

        if reading is not None:
            temp_text = format_temp(reading.temp_c, self.unit)
            highest = self.tracker.get_highest_for_mode(self.selected_sensor_id, self.max_mode)
            highest_text = format_temp(
                highest if highest is not None else reading.temp_c,
                self.unit,
            )
            short_title = (
                "Maior temp"
                if self.selected_sensor_id == MAX_VIRTUAL_ID
                else reading.display_name
            )
            max_tag = "Máx (60s)" if self.max_mode == "60s" else "Máx (sessão)"
            if self.show_max_in_tray:
                self.indicator.set_label(
                    f" {temp_text} · Máx {highest_text}",
                    " 100°C · Máx 100°C",
                )
            else:
                self.indicator.set_label(f" {temp_text}", " 100°C")
            self._summary_item.set_label(f"{short_title}: {temp_text}  •  {max_tag}: {highest_text}")

            icon_name = icon_name_for_temp(reading.temp_c)
            if icon_name != self._last_icon_name:
                self.indicator.set_icon_full(icon_name, f"Temperatura {temp_text}")
                self._last_icon_name = icon_name
        else:
            self.indicator.set_label(" --°C", " 100°C")
            self._summary_item.set_label("Nenhum sensor disponível")

        for sensor_id, item in self._sensor_items.items():
            sensor_reading = self.tracker.get_reading(sensor_id)
            if sensor_reading is None:
                continue
            val = format_temp(sensor_reading.temp_c, self.unit)
            if sensor_id == MAX_VIRTUAL_ID:
                item.set_label(f"Maior temperatura (Automático) — {val}")
            else:
                item.set_label(f"{sensor_reading.display_name} — {val}")

        if self._window is not None and self._window.get_visible():
            self._window.refresh(self.tracker, self.selected_sensor_id, self.unit, self.max_mode)

    def _select_sensor(self, sensor_id: str) -> None:
        """Persist sensor selection and immediately update tray and window."""
        if sensor_id == self.selected_sensor_id:
            return
        self.config["sensor_id"] = sensor_id
        save_config(self.config)

        item = self._sensor_items.get(sensor_id)
        if item is not None and not item.get_active():
            self._updating_menu = True
            try:
                item.set_active(True)
            finally:
                self._updating_menu = False

        self._refresh_ui()

    def _on_sensor_toggled(self, item: Gtk.RadioMenuItem, sensor_id: str) -> None:
        if self._updating_menu or not item.get_active():
            return
        self._select_sensor(sensor_id)

    def _on_interval_toggled(self, item: Gtk.RadioMenuItem, seconds: int) -> None:
        if self._updating_menu or not item.get_active():
            return
        if seconds == self.interval_sec:
            return
        self.config["interval_sec"] = seconds
        save_config(self.config)
        self._schedule_timer()

    def _on_unit_toggled(self, item: Gtk.RadioMenuItem, unit_code: str) -> None:
        if self._updating_menu or not item.get_active():
            return
        if unit_code == self.unit:
            return
        self.config["unit"] = unit_code
        save_config(self.config)
        self._refresh_ui()

    def _on_show_max_toggled(self, item: Gtk.CheckMenuItem) -> None:
        if self._updating_menu:
            return
        enabled = bool(item.get_active())
        if enabled == self.show_max_in_tray:
            return
        self.config["show_max_in_tray"] = enabled
        save_config(self.config)
        self._refresh_ui()

    def _on_max_mode_toggled(self, item: Gtk.RadioMenuItem, mode_code: str) -> None:
        if self._updating_menu or not item.get_active():
            return
        if mode_code == self.max_mode:
            return
        self.config["max_mode"] = mode_code
        save_config(self.config)
        self._refresh_ui()

    def _on_open_window(self, _item: Gtk.MenuItem) -> None:
        if self._window is None:
            self._window = TemperatureWindow(on_select_sensor=self._select_sensor)
        self._window.refresh(self.tracker, self.selected_sensor_id, self.unit, self.max_mode)
        self._window.show_all()
        self._window.present()

    def _on_reset_highest(self, _item: Gtk.MenuItem) -> None:
        self.tracker.reset_highest()
        self._refresh_ui()

    def _on_remove_shortcut_and_quit(self, _item: Gtk.MenuItem) -> None:
        remove_desktop_entry()
        self.quit()

    def _on_quit_clicked(self, _item: Gtk.MenuItem) -> None:
        self.quit()

    def _on_unix_signal(self) -> bool:
        self.quit()
        return GLib.SOURCE_REMOVE

    def quit(self) -> None:
        """Stop polling timer and exit the GTK main loop cleanly."""
        if self._timeout_id is not None:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = None
        if Gtk.main_level() > 0:
            Gtk.main_quit()


def print_sensors_report() -> int:
    """Poll hardware sensors once, print diagnostics, and return exit code."""
    tracker = SensorTracker()
    readings = tracker.poll()
    cpu_model = read_cpu_model()
    default_id = tracker.get_default_sensor_id()

    if cpu_model:
        print(f"CPU: {cpu_model}")
    print(f"Sensor padrão: {default_id}")
    if not readings:
        print("Nenhum sensor de temperatura encontrado em /sys/class/hwmon ou /sys/class/thermal.")
        return 1

    for reading in readings:
        temp_c = format_temp(reading.temp_c, "C")
        temp_f = format_temp(reading.temp_c, "F")
        icon = icon_name_for_temp(reading.temp_c)
        extra = f" | Crít: {format_temp(reading.crit_c, 'C')}" if reading.crit_c is not None else ""
        model = f" | Modelo: {reading.device_model}" if reading.device_model else ""
        marker = " [padrão]" if reading.sensor_id == default_id else ""
        print(f"- {reading.display_name} ({reading.sensor_id}): {temp_c} ({temp_f}) [{icon}]{extra}{model}{marker}")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(description="Indicador de temperatura na bandeja do Ubuntu.")
    parser.add_argument(
        "--print",
        action="store_true",
        dest="print_only",
        help="Lê os sensores uma vez, exibe no terminal e encerra.",
    )
    parser.add_argument(
        "--remove-shortcut",
        action="store_true",
        dest="remove_shortcut",
        help="Remove o atalho ~/.local/share/applications/temprl.desktop e encerra.",
    )
    return parser.parse_args(argv)


def remove_desktop_entry() -> bool:
    """Remove the user .desktop launcher and any autostart copy if present."""
    removed = False
    autostart_path = Path.home() / ".config" / "autostart" / "temprl.desktop"
    for path in (DESKTOP_ENTRY_PATH, autostart_path):
        try:
            if path.exists() or path.is_symlink():
                path.unlink()
                removed = True
        except OSError:
            pass
    return removed


def ensure_desktop_entry() -> bool:
    """Create or update the user .desktop launcher so Ubuntu Apps & Startup Apps always list Temprl."""
    launcher_script = APP_DIR / "temprl.sh"
    icon_file = ICONS_DIR / "temprl-cool.svg"

    for executable in (launcher_script, Path(__file__).resolve()):
        try:
            executable.chmod(executable.stat().st_mode | 0o755)
        except OSError:
            pass

    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Temprl\n"
        "GenericName=Monitor de Temperatura\n"
        "Comment=Mostra a temperatura dos sensores na bandeja do sistema\n"
        f"TryExec={launcher_script}\n"
        f'Exec=/usr/bin/env bash "{launcher_script}" --background\n'
        f"Icon={icon_file}\n"
        "Terminal=false\n"
        "Categories=System;Monitor;\n"
        "StartupNotify=false\n"
    )
    try:
        if DESKTOP_ENTRY_PATH.is_file() and DESKTOP_ENTRY_PATH.read_text(encoding="utf-8") == content:
            return False
        DESKTOP_ENTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        DESKTOP_ENTRY_PATH.write_text(content, encoding="utf-8")
        return True
    except OSError:
        return False


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point for temprl."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.print_only:
        return print_sensors_report()
    if args.remove_shortcut:
        remove_desktop_entry()
        print("Atalho do Temprl removido.")
        return 0

    lock = SingleInstanceLock(LOCK_PATH)
    if not lock.acquire():
        print("temprl já está em execução na bandeja do sistema.")
        return 0

    ensure_desktop_entry()
    app = TempTrayApp()
    try:
        Gtk.main()
    finally:
        app.quit()
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
