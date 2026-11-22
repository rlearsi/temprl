"""GTK 3 + Cairo live temperature chart window inspired by GNOME Resources."""

from __future__ import annotations

import io
import math
from typing import Callable, Optional

import cairo
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GdkPixbuf, Gtk  # noqa: E402

from sensors import (  # noqa: E402
    MAX_VIRTUAL_ID,
    SensorTracker,
    convert_temp,
    format_temp,
    format_temp_precise,
    read_cpu_model,
)

WINDOW_CSS = b"""
window.temprl-window {
    background-color: #1e1e1e;
    color: #f2f2f2;
}
.temprl-card {
    background-color: #2b2b2b;
    border: 1px solid #3a3a3a;
    border-radius: 12px;
    padding: 14px;
}
.temprl-title {
    font-size: 16px;
    font-weight: 700;
    color: #ffffff;
}
.temprl-subtitle {
    font-size: 11px;
    color: #9e9e9e;
}
.temprl-big-temp {
    font-size: 20px;
    font-weight: 700;
    color: #ffffff;
}
.temprl-muted {
    font-size: 13px;
    color: #b0b0b0;
}
.temprl-section {
    font-size: 12px;
    font-weight: 700;
    color: #a8a8a8;
    margin-top: 4px;
}
.temprl-sensor-row {
    background-color: #262626;
    border: 1px solid #353535;
    border-radius: 8px;
    padding: 8px 12px;
}
.temprl-sensor-row-selected {
    background-color: #283344;
    border: 1px solid #3b82f6;
    border-radius: 8px;
    padding: 8px 12px;
}
.temprl-sensor-name {
    font-size: 13px;
    font-weight: 600;
    color: #f2f2f2;
}
.temprl-sensor-val {
    font-size: 13px;
    font-weight: 700;
    color: #ffffff;
}
"""


class TemperatureWindow(Gtk.Window):
    """Live temperature chart and sensor summary window."""

    def __init__(
        self,
        on_select_sensor: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(title="Monitor de Temperatura")
        self.set_default_size(500, 440)
        self.set_border_width(14)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.get_style_context().add_class("temprl-window")

        self._on_select_sensor = on_select_sensor
        self._history: list[tuple[float, float]] = []
        self._unit = "C"
        self._row_widgets: dict[str, tuple[Gtk.EventBox, Gtk.Label, Gtk.Label]] = {}

        self._apply_css()
        self._build_ui()
        self.connect("delete-event", self._on_delete_event)

    def _apply_css(self) -> None:
        """Load dark card theme styling for this window."""
        provider = Gtk.CssProvider()
        provider.load_from_data(WINDOW_CSS)
        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(
                screen,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def _build_ui(self) -> None:
        """Create header, chart card, and sensor list widgets."""
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add(root)

        header_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._title_label = Gtk.Label(label="Sensores de Temperatura", xalign=0.0)
        self._title_label.get_style_context().add_class("temprl-title")
        header_box.pack_start(self._title_label, False, False, 0)

        cpu_model = read_cpu_model()
        subtitle_text = cpu_model if cpu_model else "Leitura em tempo real (/sys/class/hwmon)"
        self._subtitle_label = Gtk.Label(label=subtitle_text, xalign=0.0)
        self._subtitle_label.get_style_context().add_class("temprl-subtitle")
        header_box.pack_start(self._subtitle_label, False, False, 0)
        root.pack_start(header_box, False, False, 0)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.get_style_context().add_class("temprl-card")

        self._chart_image = Gtk.Image()
        self._chart_image.set_size_request(440, 175)
        self._chart_image.connect("size-allocate", self._on_chart_size_allocate)
        card.pack_start(self._chart_image, True, True, 0)

        stats_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._sensor_caption = Gtk.Label(label="Temperatura", xalign=0.0)
        self._sensor_caption.get_style_context().add_class("temprl-muted")
        stats_box.pack_start(self._sensor_caption, False, False, 0)

        self._summary_label = Gtk.Label(label="--°C · Máxima: --°C", xalign=0.0)
        self._summary_label.get_style_context().add_class("temprl-big-temp")
        stats_box.pack_start(self._summary_label, False, False, 0)
        card.pack_start(stats_box, False, False, 0)

        root.pack_start(card, False, False, 0)

        section_label = Gtk.Label(label="Todos os sensores detectados", xalign=0.0)
        section_label.get_style_context().add_class("temprl-section")
        root.pack_start(section_label, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(110)

        self._rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        scroll.add(self._rows_box)
        root.pack_start(scroll, True, True, 0)

    def _on_delete_event(self, _widget: Gtk.Widget, _event: Gdk.Event) -> bool:
        """Hide window instead of destroying it so the tray indicator stays alive."""
        self.hide()
        return True

    def _ensure_sensor_rows(self, tracker: SensorTracker) -> None:
        """Create list rows once for every detected physical sensor."""
        existing_ids = set(self._row_widgets.keys())
        current_ids = set(tracker.ordered_ids)
        if existing_ids == current_ids:
            return

        for child in self._rows_box.get_children():
            self._rows_box.remove(child)
        self._row_widgets.clear()

        for sensor_id in tracker.ordered_ids:
            reading = tracker.get_reading(sensor_id)
            if reading is None:
                continue

            event_box = Gtk.EventBox()
            event_box.get_style_context().add_class("temprl-sensor-row")
            event_box.connect("button-press-event", self._on_row_clicked, sensor_id)

            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            name_lbl = Gtk.Label(label=reading.display_name, xalign=0.0)
            name_lbl.get_style_context().add_class("temprl-sensor-name")
            if reading.device_model:
                event_box.set_tooltip_text(reading.device_model)

            val_lbl = Gtk.Label(label="--", xalign=1.0)
            val_lbl.get_style_context().add_class("temprl-sensor-val")

            row.pack_start(name_lbl, True, True, 0)
            row.pack_end(val_lbl, False, False, 0)
            event_box.add(row)
            self._rows_box.pack_start(event_box, False, False, 0)
            self._row_widgets[sensor_id] = (event_box, name_lbl, val_lbl)

        self._rows_box.show_all()

    def _on_row_clicked(
        self,
        _widget: Gtk.Widget,
        _event: Gdk.EventButton,
        sensor_id: str,
    ) -> bool:
        """Allow clicking a sensor row to switch the monitored sensor."""
        if self._on_select_sensor is not None:
            self._on_select_sensor(sensor_id)
        return True

    def refresh(
        self,
        tracker: SensorTracker,
        selected_id: str,
        unit: str,
        max_mode: str = "session",
    ) -> None:
        """Update chart data, headline values, and per-sensor rows."""
        self._unit = unit
        max_label = "Máxima (60s)" if max_mode == "60s" else "Máxima"
        reading = tracker.get_reading(selected_id)
        if reading is not None:
            highest = tracker.get_highest_for_mode(selected_id, max_mode)
            highest_str = format_temp(highest if highest is not None else reading.temp_c, unit)
            current_str = format_temp(reading.temp_c, unit)
            label_title = (
                "Maior temperatura" if selected_id == MAX_VIRTUAL_ID else reading.display_name
            )
            self._sensor_caption.set_text(label_title)
            self._summary_label.set_text(f"{current_str} · {max_label}: {highest_str}")
            self._history = tracker.get_history(selected_id)
        else:
            self._sensor_caption.set_text("Sensor indisponível")
            self._summary_label.set_text("--")
            self._history = []

        self._ensure_sensor_rows(tracker)
        for sensor_id, (event_box, _name_lbl, val_lbl) in self._row_widgets.items():
            ctx = event_box.get_style_context()
            if sensor_id == selected_id:
                ctx.remove_class("temprl-sensor-row")
                ctx.add_class("temprl-sensor-row-selected")
            else:
                ctx.remove_class("temprl-sensor-row-selected")
                ctx.add_class("temprl-sensor-row")

            row_reading = tracker.get_reading(sensor_id)
            if row_reading is None:
                continue
            row_high = tracker.get_highest_for_mode(sensor_id, max_mode)
            parts = [format_temp_precise(row_reading.temp_c, unit)]
            if row_high is not None:
                parts.append(f"Máx: {format_temp(row_high, unit)}")
            if row_reading.crit_c is not None:
                parts.append(f"Crít: {format_temp(row_reading.crit_c, unit)}")
            val_lbl.set_text(" · ".join(parts))

        self._render_chart()

    def _on_chart_size_allocate(
        self,
        _widget: Gtk.Widget,
        allocation: Gdk.Rectangle,
    ) -> None:
        """Re-render chart when window width changes significantly."""
        current_pb = self._chart_image.get_pixbuf()
        if current_pb is None or abs(current_pb.get_width() - allocation.width) > 8:
            self._render_chart(allocation.width, allocation.height)

    def _render_chart(
        self,
        alloc_w: Optional[int] = None,
        alloc_h: Optional[int] = None,
    ) -> None:
        """Render the Resources-style blue area + line graph via Cairo ImageSurface -> GdkPixbuf."""
        width = max(440, alloc_w or self._chart_image.get_allocated_width())
        height = max(175, alloc_h or self._chart_image.get_allocated_height())

        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        cr = cairo.Context(surface)

        # Dark chart background with subtle rounded corners
        radius = 8.0
        cr.new_sub_path()
        cr.arc(width - radius, radius, radius, -math.pi / 2, 0)
        cr.arc(width - radius, height - radius, radius, 0, math.pi / 2)
        cr.arc(radius, height - radius, radius, math.pi / 2, math.pi)
        cr.arc(radius, radius, radius, math.pi, 3 * math.pi / 2)
        cr.close_path()
        cr.clip()

        cr.set_source_rgb(0.18, 0.21, 0.25)
        cr.paint()

        values = [convert_temp(temp_c, self._unit) for _, temp_c in self._history]
        if values:
            min_val = min(values)
            max_val = max(values)
            span = max(10.0, max_val - min_val)
            y_low = math.floor((min_val - span * 0.2) / 5.0) * 5.0
            y_high = math.ceil((max_val + span * 0.2) / 5.0) * 5.0
            if y_high <= y_low:
                y_high = y_low + 10.0

            pad_top = 12.0
            pad_bottom = 10.0
            plot_h = max(1.0, height - pad_top - pad_bottom)

            # Horizontal reference gridlines
            cr.set_line_width(1.0)
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.08)
            for step in (0.25, 0.5, 0.75):
                gy = pad_top + plot_h * step
                cr.move_to(0, gy)
                cr.line_to(width, gy)
                cr.stroke()

            # Map samples to pixel coordinates
            count = len(values)
            points: list[tuple[float, float]] = []
            if count == 1:
                y = pad_top + plot_h * (1.0 - (values[0] - y_low) / (y_high - y_low))
                points = [(0.0, y), (float(width), y)]
            else:
                for idx, val in enumerate(values):
                    x = (idx / (count - 1)) * width
                    norm = (val - y_low) / (y_high - y_low)
                    y = pad_top + plot_h * (1.0 - max(0.0, min(1.0, norm)))
                    points.append((x, y))

            # Filled area under the curve
            cr.move_to(points[0][0], height)
            for x, y in points:
                cr.line_to(x, y)
            cr.line_to(points[-1][0], height)
            cr.close_path()
            cr.set_source_rgba(0.17, 0.42, 0.69, 0.48)
            cr.fill()

            # Crisp blue top line
            cr.move_to(points[0][0], points[0][1])
            for x, y in points[1:]:
                cr.line_to(x, y)
            cr.set_source_rgb(0.23, 0.51, 0.96)
            cr.set_line_width(2.0)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            cr.stroke()

            # Subtle min/max scale labels in top-left and bottom-left
            unit_suffix = "°F" if self._unit.upper() == "F" else "°C"
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(10.5)
            cr.set_source_rgba(0.88, 0.91, 0.96, 0.65)
            cr.move_to(8, 15)
            cr.show_text(f"{round(y_high)}{unit_suffix}")
            cr.move_to(8, height - 8)
            cr.show_text(f"{round(y_low)}{unit_suffix}")

        buf = io.BytesIO()
        surface.write_to_png(buf)
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(buf.getvalue())
        loader.close()
        pixbuf = loader.get_pixbuf()
        if pixbuf is not None:
            self._chart_image.set_from_pixbuf(pixbuf)
