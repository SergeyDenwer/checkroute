"""
Route card renderer — pycairo-based PNG generation.

Usage:
    from route_card import RouteCardRenderer, RouteCardData, ForecastRow, compute_condition_index

    data = RouteCardData(
        route_name="Гравийная Муха",
        length_km=23.4,
        soil_name="Суглинок",
        condition_index=compute_condition_index(dry_pct=10, wet_pct=20, mud_pct=50, swamp_pct=20),
        verdict_text="НЕЛЬЗЯ",
        verdict_level=1,
        forecast_rows=[
            ForecastRow(level=1, label="НЕЛЬЗЯ",        date_str="сегодня"),
            ForecastRow(level=2, label="СКОРЕЕ НЕЛЬЗЯ", date_str="10.03 (через 6 дней)"),
            ForecastRow(level=3, label="СКОРЕЕ МОЖНО",  date_str="16.03 (через 12 дней)"),
        ],
    )
    png_bytes = RouteCardRenderer().render(data)
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import cairo

Color = Tuple[float, float, float]

# Russian display names for soil types
SOIL_DISPLAY = {
    "sand":       "Песок",
    "sandy_loam": "Супесь",
    "loam":       "Суглинок",
    "silt_loam":  "Илистый суглинок",
    "clay_loam":  "Глинистый суглинок",
    "clay":       "Глина",
    "chernozem":  "Чернозём",
}


# ─────────────────────────────── Data models ─────────────────────────────────

@dataclass
class ForecastRow:
    """Single row in the forecast section."""
    level:    int   # 1 = can't ride … 4 = can ride
    label:    str   # "НЕЛЬЗЯ", "СКОРЕЕ МОЖНО", …
    date_str: str   # "сегодня"  or  "12.03 (через 8 дней)"


@dataclass
class RouteCardData:
    route_name:      str
    length_km:       float          # kilometres, e.g. 23.4
    soil_name:       str            # human-readable, e.g. "Суглинок"
    condition_index: int            # 0 (perfectly dry) → 100 (fully swamped)
    verdict_text:    str            # "НЕЛЬЗЯ" / "СКОРЕЕ НЕЛЬЗЯ" / "СКОРЕЕ МОЖНО" / "МОЖНО"
    verdict_level:   int            # 1 – 4
    forecast_rows:   List[ForecastRow] = field(default_factory=list)


def compute_condition_index(
    dry_pct: float,
    wet_pct: float,
    mud_pct: float,
    swamp_pct: float,
) -> int:
    """
    Map status distribution → 0-100 condition index.
    0 = fully dry and rideable, 100 = total swamp.
    """
    raw = wet_pct * 0.40 + mud_pct * 0.75 + swamp_pct * 1.00
    return min(100, max(0, round(raw)))


# ──────────────────────────────── Renderer ───────────────────────────────────

class RouteCardRenderer:
    """Renders a single-route condition card as PNG bytes via pycairo."""

    WIDTH  = 800
    H_PAD  = 30

    # ── Palette ───────────────────────────────────────────────────────────────
    BG      : Color = (0.04,  0.04,  0.04)
    CARD    : Color = (0.083, 0.083, 0.083)
    WHITE   : Color = (1.0,   1.0,   1.0)
    GRAY    : Color = (0.58,  0.58,  0.58)
    DIVIDER : Color = (0.16,  0.16,  0.16)

    VERDICT_COLOR = {
        1: (0.93, 0.20, 0.20),   # red
        2: (1.00, 0.58, 0.00),   # orange
        3: (0.35, 0.85, 0.25),   # green
        4: (0.10, 0.95, 0.10),   # bright green
    }

    # ── Public API ────────────────────────────────────────────────────────────

    def render(self, data: RouteCardData) -> bytes:
        """Return PNG image as bytes."""
        total_h = self._total_height(data)

        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.WIDTH, total_h)
        ctx = cairo.Context(surface)

        ctx.set_source_rgb(*self.BG)
        ctx.paint()

        y = 0
        y = self._draw_header(ctx, data, y)
        y = self._draw_speedometer_section(ctx, data, y)
        if data.forecast_rows:
            self._draw_forecast_section(ctx, data, y)

        out = io.BytesIO()
        surface.write_to_png(out)
        return out.getvalue()

    # ── Height calculation ────────────────────────────────────────────────────

    def _total_height(self, data: RouteCardData) -> int:
        h = 110                                             # header
        h += 448                                            # speedometer card
        if data.forecast_rows:
            h += 30 + 16 + len(data.forecast_rows) * 56 + 20   # forecast
        h += 40                                             # bottom padding
        return h

    # ── Section: header ───────────────────────────────────────────────────────

    def _draw_header(self, ctx, data: RouteCardData, y0: int) -> int:
        h = 110

        # Background stripe
        ctx.set_source_rgb(*self.BG)
        ctx.rectangle(0, y0, self.WIDTH, h)
        ctx.fill()

        # Bottom separator
        ctx.set_source_rgb(*self.DIVIDER)
        ctx.rectangle(0, y0 + h - 1, self.WIDTH, 1)
        ctx.fill()

        cx = self.WIDTH / 2
        self._text(ctx, data.route_name,
                   cx, y0 + 52, size=36, bold=True, align='center')

        subtitle = f"{data.length_km:.1f} km  ·  {data.soil_name}"
        self._text(ctx, subtitle,
                   cx, y0 + 85, size=21, align='center', color=self.GRAY)

        return y0 + h

    # ── Section: speedometer card ─────────────────────────────────────────────

    def _draw_speedometer_section(self, ctx, data: RouteCardData, y0: int) -> int:
        card_x = self.H_PAD
        card_y = y0 + 28
        card_w = self.WIDTH - self.H_PAD * 2
        card_h = 420

        ctx.set_source_rgb(*self.CARD)
        self._rounded_rect(ctx, card_x, card_y, card_w, card_h, r=14)
        ctx.fill()

        cx = self.WIDTH / 2
        cy = card_y + 240
        self._draw_speedometer(ctx, cx, cy, data.condition_index)

        vc = self.VERDICT_COLOR[data.verdict_level]
        self._text(ctx, data.verdict_text,
                   cx, cy + 128, size=26, bold=True, align='center', color=vc)

        return y0 + 28 + card_h

    # ── Section: forecast ─────────────────────────────────────────────────────

    def _draw_forecast_section(self, ctx, data: RouteCardData, y0: int) -> int:
        pad     = self.H_PAD
        row_h   = 56
        title_y = y0 + 30
        card_y  = title_y + 16
        card_w  = self.WIDTH - pad * 2
        card_h  = len(data.forecast_rows) * row_h + 20

        self._text(ctx, "Когда можно ехать:",
                   pad, title_y, size=20, color=self.GRAY)

        ctx.set_source_rgb(*self.CARD)
        self._rounded_rect(ctx, pad, card_y, card_w, card_h, r=14)
        ctx.fill()

        for i, row in enumerate(data.forecast_rows):
            ry = card_y + 10 + i * row_h

            # Divider between rows
            if i > 0:
                ctx.set_source_rgb(*self.DIVIDER)
                ctx.rectangle(pad + 18, ry, card_w - 36, 1)
                ctx.fill()

            # Colored circle indicator
            dot_cx = pad + 30
            dot_cy = ry + row_h / 2
            ctx.set_source_rgb(*self.VERDICT_COLOR[row.level])
            ctx.arc(dot_cx, dot_cy, 9, 0, 2 * math.pi)
            ctx.fill()

            # Verdict label
            text_y = dot_cy + 8
            self._text(ctx, row.label,
                       dot_cx + 24, text_y,
                       size=22, bold=True)

            # Date (right-aligned)
            self._text(ctx, row.date_str,
                       pad + card_w - 18, text_y,
                       size=21, align='right', color=self.GRAY)

        return y0 + 30 + 16 + card_h

    # ── Speedometer ───────────────────────────────────────────────────────────

    def _draw_speedometer(self, ctx, cx: float, cy: float, value: int):
        """
        Classic semi-circular speedometer.
        Arc sweeps top half from left (9 o'clock) to right (3 o'clock).
        Needle angle: π (left, value=0)  →  2π (right, value=100).
        """
        R         = 195  # arc radius
        thick     = 28   # arc stroke width
        nlen      = 162  # needle length
        tick_in   = R - 28
        tick_out  = R - 50

        # ── Arc: yellow → red gradient ────────────────────────────────────
        grad = cairo.LinearGradient(cx - R, cy, cx + R, cy)
        grad.add_color_stop_rgb(0, 1.0, 0.96, 0.0)   # #FFF500
        grad.add_color_stop_rgb(1, 1.0, 0.0,  0.0)   # #FF0000
        ctx.set_source(grad)
        ctx.set_line_width(thick)
        ctx.set_line_cap(cairo.LINE_CAP_BUTT)
        ctx.arc(cx, cy, R, math.pi, 2 * math.pi)
        ctx.stroke()

        # ── Tick marks ────────────────────────────────────────────────────
        ctx.set_source_rgb(*self.WHITE)
        ctx.set_line_width(3)
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        for i in range(6):
            a  = math.pi + i * math.pi / 5
            sx = cx + tick_in  * math.cos(a)
            sy = cy + tick_in  * math.sin(a)
            ex = cx + tick_out * math.cos(a)
            ey = cy + tick_out * math.sin(a)
            ctx.move_to(sx, sy)
            ctx.line_to(ex, ey)
            ctx.stroke()

        # ── Needle ────────────────────────────────────────────────────────
        na = math.pi + (value / 100) * math.pi
        ctx.set_source_rgb(*self.WHITE)
        ctx.set_line_width(4)
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        ctx.move_to(cx, cy)
        ctx.line_to(cx + nlen * math.cos(na),
                    cy + nlen * math.sin(na))
        ctx.stroke()

        # ── Center cap ────────────────────────────────────────────────────
        ctx.set_source_rgb(*self.WHITE)
        ctx.arc(cx, cy, 11, 0, 2 * math.pi)
        ctx.fill()

        # ── Percentage label ──────────────────────────────────────────────
        self._text(ctx, f"{value}%",
                   cx, cy + 78, size=52, bold=True, align='center')

    # ── Drawing helpers ───────────────────────────────────────────────────────

    def _rounded_rect(self, ctx, x: float, y: float,
                      w: float, h: float, r: float):
        """Add a rounded-rectangle path to ctx."""
        ctx.new_sub_path()
        ctx.arc(x + w - r, y + r,     r, -math.pi / 2, 0)
        ctx.arc(x + w - r, y + h - r, r,  0,            math.pi / 2)
        ctx.arc(x + r,     y + h - r, r,  math.pi / 2,  math.pi)
        ctx.arc(x + r,     y + r,     r,  math.pi,       3 * math.pi / 2)
        ctx.close_path()

    def _text(self, ctx, text: str, x: float, y: float,
              size: int = 24,
              bold: bool = False,
              align: str = 'left',
              color: Optional[Color] = None):
        """Draw text with optional alignment and color."""
        ctx.set_source_rgb(*(color or self.WHITE))
        weight = cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL
        ctx.select_font_face("Noto Sans", cairo.FONT_SLANT_NORMAL, weight)
        ctx.set_font_size(size)

        xb, _, tw, _, _, _ = ctx.text_extents(text)
        if align == 'center':
            x -= xb + tw / 2
        elif align == 'right':
            x -= xb + tw
        else:
            x -= xb

        ctx.move_to(x, y)
        ctx.show_text(text)
