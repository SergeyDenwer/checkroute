"""
Route card renderer — pycairo-based PNG generation.

Usage:
    from route_card import RouteCardRenderer, RouteCardData, ForecastRow, compute_condition_index

    data = RouteCardData(
        route_name="Гравийная Муха",
        length_km=23.4,
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
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import cairo

Color = Tuple[float, float, float]


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
    condition_index: int            # 0 (perfectly dry) → 100 (fully swamped)
    verdict_text:    str            # "НЕЛЬЗЯ" / "СКОРЕЕ НЕЛЬЗЯ" / "СКОРЕЕ МОЖНО" / "МОЖНО"
    verdict_level:   int            # 1 – 4
    dry_pct:         float = 0.0   # status distribution, %
    wet_pct:         float = 0.0
    mud_pct:         float = 0.0
    swamp_pct:       float = 0.0
    forecast_rows:   List[ForecastRow] = field(default_factory=list)
    points_sampled:  int   = 0     # total sampled checkpoints
    points_analyzed: int   = 0     # after skipping paved ones


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


# Verdict thresholds. Override via env: CI_CAN_RIDE, CI_PROBABLY_CAN, CI_PROBABLY_CANNOT
def verdict_from_ci(ci: int) -> tuple:
    """
    CI-based verdict. Returns (emoji_text, level), level 1 (нельзя) … 4 (можно).
    Thresholds read from env each call so restart-free tuning works.
    """
    ci_can      = int(os.getenv("CI_CAN_RIDE",        "25"))
    ci_prob_can = int(os.getenv("CI_PROBABLY_CAN",    "45"))
    ci_prob_no  = int(os.getenv("CI_PROBABLY_CANNOT", "65"))
    if ci <= ci_can:       return "✅ МОЖНО",          4
    if ci <= ci_prob_can:  return "🟢 СКОРЕЕ МОЖНО",   3
    if ci <= ci_prob_no:   return "🟠 СКОРЕЕ НЕЛЬЗЯ",  2
    return                        "🔴 НЕЛЬЗЯ",          1


# ──────────────────────────────── Renderer ───────────────────────────────────

class RouteCardRenderer:
    """Renders a single-route condition card as PNG bytes via pycairo."""

    WIDTH  = 540
    H_PAD  = 20

    # ── Palette ───────────────────────────────────────────────────────────────
    BG      : Color = (0.04,  0.04,  0.04)
    CARD    : Color = (0.083, 0.083, 0.083)
    WHITE   : Color = (1.0,   1.0,   1.0)
    GRAY    : Color = (0.58,  0.58,  0.58)
    DIVIDER : Color = (0.16,  0.16,  0.16)

    # Single source of truth for the 4-level status palette (best → worst).
    # Tailwind: emerald-400 → amber-400 → orange-500 → red-500
    _P: tuple = (
        (0.204, 0.827, 0.600),   # #34D399  green
        (0.984, 0.749, 0.141),   # #FBBF24  yellow
        (0.976, 0.451, 0.086),   # #F97316  orange
        (0.937, 0.267, 0.267),   # #EF4444  red
    )
    # Level 0 — rain forecast: Tailwind blue-400
    _RAIN_COLOR: Color = (0.243, 0.631, 0.957)  # #3EA1F4

    @property
    def _STATUS_ROWS(self):
        p = self._P
        return [
            ("dry_pct",   "СУХО",   p[0]),
            ("wet_pct",   "ВЛАЖНО", p[1]),
            ("mud_pct",   "ГРЯЗЬ",  p[2]),
            ("swamp_pct", "МЕСИВО", p[3]),
        ]

    @property
    def VERDICT_COLOR(self):
        p = self._P
        return {4: p[0], 3: p[1], 2: p[2], 1: p[3], 0: self._RAIN_COLOR}

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
        # rows_start - card_y = (38+34) + 126 = 198; card_h = 198 + 168 + 16 = 382
        h = 110          # header
        h += 20 + 382    # scale + status merged card
        if data.forecast_rows:
            h += 38 + 6 + len(data.forecast_rows) * 56 + 20
        h += 40          # bottom padding
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
                   cx, y0 + 52, size=30, bold=True, align='center')

        pts = f"{data.points_analyzed} из {data.points_sampled} точек" if data.points_sampled else ""
        subtitle = f"{data.length_km:.1f} km" + (f"  ·  {pts}" if pts else "")
        self._text(ctx, subtitle,
                   cx, y0 + 82, size=18, align='center', color=self.GRAY)

        return y0 + h

    # ── Section: condition scale card ────────────────────────────────────────

    def _draw_speedometer_section(self, ctx, data: RouteCardData, y0: int) -> int:
        pad    = self.H_PAD
        card_x = pad
        card_y = y0 + 20
        card_w = self.WIDTH - pad * 2
        cx     = self.WIDTH / 2

        bar_x = card_x + 50
        bar_w = card_w - 100
        bar_y = card_y + 38
        bar_h = 34
        bb    = bar_y + bar_h          # bar bottom

        status_row_h = 42
        sb_x  = card_x + 170
        sb_w  = card_w - 170 - 90
        sb_bh = 10
        pct_x = card_x + card_w - 14

        rows_start = bb + 126
        card_h     = (rows_start - card_y) + 4 * status_row_h + 16

        # Card background
        ctx.set_source_rgb(*self.CARD)
        self._rounded_rect(ctx, card_x, card_y, card_w, card_h, r=14)
        ctx.fill()

        # ── Condition scale bar ────────────────────────────────────────────────
        self._draw_condition_scale(ctx, bar_x, bar_y, bar_w, bar_h,
                                   data.condition_index)

        self._text(ctx, "СУХО",   bar_x,         bb + 28, size=15, color=self.GRAY)
        self._text(ctx, "МЕСИВО", bar_x + bar_w, bb + 28,
                   size=15, align='right', color=self.GRAY)

        self._text(ctx, f"{data.condition_index}%",
                   cx, bb + 68, size=36, bold=True, align='center')

        vc = self.VERDICT_COLOR[data.verdict_level]
        self._text(ctx, data.verdict_text,
                   cx, bb + 88, size=18, bold=True, align='center', color=vc)

        # "Состояние:" — small label just above first row dots, same left indent
        self._text(ctx, "Состояние:", card_x + 14, rows_start - 4,
                   size=20, color=self.GRAY)

        # ── Status bars (no dividers) ─────────────────────────────────────────
        for i, (field_name, label, color) in enumerate(self._STATUS_ROWS):
            pct   = getattr(data, field_name)
            ry    = rows_start + i * status_row_h
            mid_y = ry + status_row_h / 2
            text_y = mid_y + 7

            ctx.set_source_rgb(*color)
            ctx.arc(card_x + 22, mid_y, 7, 0, 2 * math.pi)
            ctx.fill()

            self._text(ctx, label, card_x + 38, text_y, size=18, bold=True)

            by = mid_y - sb_bh / 2
            ctx.set_source_rgb(0.12, 0.12, 0.12)
            self._rounded_rect(ctx, sb_x, by, sb_w, sb_bh, sb_bh / 2)
            ctx.fill()

            fill_w = max(sb_bh, sb_w * pct / 100)
            ctx.set_source_rgb(*color)
            self._rounded_rect(ctx, sb_x, by, fill_w, sb_bh, sb_bh / 2)
            ctx.fill()

            self._text(ctx, f"{pct:.0f}%", pct_x, text_y,
                       size=17, align='right', color=self.GRAY)

        return y0 + 20 + card_h

    # ── Section: forecast ─────────────────────────────────────────────────────

    def _draw_forecast_section(self, ctx, data: RouteCardData, y0: int) -> int:
        pad     = self.H_PAD
        row_h   = 56
        title_y = y0 + 38
        card_y  = title_y + 6
        card_w  = self.WIDTH - pad * 2
        card_h  = len(data.forecast_rows) * row_h + 20

        self._text(ctx, "Прогноз:",
                   pad + 4, title_y, size=20, color=self.GRAY)

        for i, row in enumerate(data.forecast_rows):
            ry = card_y + i * row_h

            # Divider between rows
            if i > 0:
                ctx.set_source_rgb(*self.DIVIDER)
                ctx.rectangle(pad + 14, ry, card_w - 28, 1)
                ctx.fill()

            # Colored circle indicator
            dot_cx = pad + 22
            dot_cy = ry + row_h / 2
            ctx.set_source_rgb(*self.VERDICT_COLOR[row.level])
            ctx.arc(dot_cx, dot_cy, 7, 0, 2 * math.pi)
            ctx.fill()

            # Verdict label
            text_y = dot_cy + 7
            self._text(ctx, row.label,
                       dot_cx + 18, text_y,
                       size=19, bold=True)

            # Date (right-aligned)
            self._text(ctx, row.date_str,
                       pad + card_w - 12, text_y,
                       size=17, align='right', color=self.GRAY)

        return y0 + 30 + 16 + card_h

    # ── Condition scale (horizontal bar) ─────────────────────────────────────

    def _draw_condition_scale(self, ctx,
                              bx: float, by: float,
                              bw: float, bh: float,
                              value: int):
        """
        Horizontal gradient bar: green (dry, left) → red (swamp, right).
        A white pin marks the current condition_index (0–100).
        """
        r = bh / 2   # rounded-end radius

        # ── Gradient bar — stops match _P palette ─────────────────────────
        p = self._P
        grad = cairo.LinearGradient(bx, 0, bx + bw, 0)
        grad.add_color_stop_rgb(0.00, *p[0])   # green
        grad.add_color_stop_rgb(0.33, *p[1])   # yellow
        grad.add_color_stop_rgb(0.67, *p[2])   # orange
        grad.add_color_stop_rgb(1.00, *p[3])   # red
        ctx.set_source(grad)
        self._rounded_rect(ctx, bx, by, bw, bh, r)
        ctx.fill()

        # ── Subtle inner shadow / depth line ─────────────────────────────
        ctx.set_source_rgba(0, 0, 0, 0.18)
        self._rounded_rect(ctx, bx, by, bw, bh, r)
        ctx.set_line_width(2)
        ctx.stroke()

        # ── Marker pin ───────────────────────────────────────────────────
        mx = bx + bw * (value / 100)
        mx = max(bx + r, min(bx + bw - r, mx))   # clamp to bar bounds

        pin_top    = by - 18
        pin_bottom = by + bh + 14
        pin_r      = 7

        # Shadow behind pin
        ctx.set_source_rgba(0, 0, 0, 0.35)
        ctx.set_line_width(5)
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        ctx.move_to(mx, pin_top + 2)
        ctx.line_to(mx, pin_bottom + 2)
        ctx.stroke()

        # White line
        ctx.set_source_rgb(*self.WHITE)
        ctx.set_line_width(3)
        ctx.move_to(mx, pin_top)
        ctx.line_to(mx, pin_bottom)
        ctx.stroke()

        # Circle at top
        ctx.set_source_rgb(*self.WHITE)
        ctx.arc(mx, pin_top, pin_r, 0, 2 * math.pi)
        ctx.fill()

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


# ══════════════════════════════════════════════════════════════════════════════
#  Batch card — multi-route summary
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BatchRouteRow:
    name:           str
    today_ci:       int    # condition index 0 (dry) → 100 (swamp)
    today_level:    int    # 1 (can't ride) … 4 (can ride)
    tomorrow_ci:    int
    tomorrow_level: int
    saturday_ci:    int
    saturday_level: int
    sunday_ci:      int
    sunday_level:   int


@dataclass
class BatchCardData:
    date_str:    str    # e.g. "10.03.2026"
    col3_label:  str    # e.g. "Сб 14.03"
    col4_label:  str    # e.g. "Вс 15.03"
    routes:      List[BatchRouteRow]

    @property
    def counts(self) -> Dict[int, int]:
        c: Dict[int, int] = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
        for r in self.routes:
            c[r.today_level] = c.get(r.today_level, 0) + 1
        return c


class BatchCardRenderer:
    """
    Renders a multi-route batch summary card as PNG bytes.

    Layout (540 px wide, dynamic height):
      ┌──────────────────────────────────────────────┐
      │  СВОДКА МАРШРУТОВ                            │  header (80)
      │  Суглинок · 10.03.2026 · 9 маршрутов         │
      ├──────────────────────────────────────────────┤
      │  ● 5 МОЖНО  ● 2 СКОРЕЕ  ● 1 ОСТОРОЖНО  ● 0  │  pills  (56)
      ├────────────────────┬───────┬───────┬─────────┤
      │ Маршрут            │Сегодня│Завтра │Сб 14.03 │  col headers (36)
      ├────────────────────┼───────┼───────┼─────────┤
      │ ● Медведки         │ ████░ │ ███░░ │ ██░░░   │  route row  (44 ea)
      │ ● Жуков лог        │ █░░░░ │ ██░░░ │ ███░░   │
      └────────────────────┴───────┴───────┴─────────┘
    """

    WIDTH   = 540
    H_PAD   = 20
    ROW_H   = 44

    BG      : Color = (0.04,  0.04,  0.04)
    WHITE   : Color = (1.0,   1.0,   1.0)
    GRAY    : Color = (0.58,  0.58,  0.58)
    DIVIDER : Color = (0.16,  0.16,  0.16)

    # Same palette as RouteCardRenderer: best(green) → worst(red)
    _P: tuple = (
        (0.204, 0.827, 0.600),   # #34D399  green
        (0.984, 0.749, 0.141),   # #FBBF24  yellow
        (0.976, 0.451, 0.086),   # #F97316  orange
        (0.937, 0.267, 0.267),   # #EF4444  red
    )
    _RAIN_COLOR: Color = (0.243, 0.631, 0.957)  # #3EA1F4  blue — rain/level 0

    # ── Colour helpers ────────────────────────────────────────────────────────

    def _level_color(self, level: int) -> Color:
        """level 4 = best (green) … level 1 = worst (red), level 0 = rain (blue)."""
        if level == 0:
            return self._RAIN_COLOR
        return self._P[4 - level]

    def _dry_color(self, pct: float) -> Color:
        if pct >= 70: return self._P[0]
        if pct >= 50: return self._P[1]
        if pct >= 30: return self._P[2]
        return self._P[3]

    # ── Public API ────────────────────────────────────────────────────────────

    SCALE = 2   # render at 2× for crisp display

    def render(self, data: BatchCardData) -> bytes:
        total_h = self._total_height(len(data.routes))
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32,
            self.WIDTH  * self.SCALE,
            total_h * self.SCALE,
        )
        ctx = cairo.Context(surface)
        ctx.scale(self.SCALE, self.SCALE)

        ctx.set_source_rgb(*self.BG)
        ctx.paint()

        y = 0
        y = self._draw_header(ctx, data, y)
        y = self._draw_pills(ctx, data, y)
        y = self._draw_col_headers(ctx, data, y)
        self._draw_rows(ctx, data, y)

        out = io.BytesIO()
        surface.write_to_png(out)
        return out.getvalue()

    # ── Height ────────────────────────────────────────────────────────────────

    def _total_height(self, n: int) -> int:
        return 80 + 68 + 36 + n * self.ROW_H + 24

    # ── Column geometry ───────────────────────────────────────────────────────

    def _col_centers(self) -> List[float]:
        """X centers for the 4 data columns (Сегодня / Завтра / Суббота / Воскресенье)."""
        name_end  = self.H_PAD + 180
        right_end = self.WIDTH - self.H_PAD
        col_w     = (right_end - name_end) / 4
        return [name_end + col_w * (i + 0.5) for i in range(4)]

    # ── Header ────────────────────────────────────────────────────────────────

    def _draw_header(self, ctx, data: BatchCardData, y0: int) -> int:
        h  = 80
        cx = self.WIDTH / 2

        self._text(ctx, "СВОДКА МАРШРУТОВ",
                   cx, y0 + 42, size=24, bold=True, align='center')

        subtitle = f"{data.date_str}  ·  {len(data.routes)} маршрутов"
        self._text(ctx, subtitle,
                   cx, y0 + 66, size=15, align='center', color=self.GRAY)

        self._divider(ctx, y0 + h - 1)
        return y0 + h

    # ── Pill row (verdict counts) ─────────────────────────────────────────────

    def _draw_pills(self, ctx, data: BatchCardData, y0: int) -> int:
        h      = 68
        counts = data.counts
        pill_info = [
            (4, "МОЖНО"),
            (3, "ВЕРОЯТНО"),
            (2, "ОСТОРОЖНО"),
            (1, "НЕЛЬЗЯ"),
            (0, "ДОЖДЬ"),
        ]
        inner_w  = self.WIDTH - self.H_PAD * 2
        seg_w    = inner_w / 5
        cy       = y0 + 20

        for i, (level, label) in enumerate(pill_info):
            color   = self._level_color(level)
            count   = counts.get(level, 0)
            seg_cx  = self.H_PAD + seg_w * (i + 0.5)

            # Dot + number horizontally centred together
            dot_x = seg_cx - 16
            ctx.set_source_rgb(*color)
            ctx.arc(dot_x, cy, 7, 0, 2 * math.pi)
            ctx.fill()

            self._text(ctx, str(count), dot_x + 14, cy + 8, size=20, bold=True)

            # Small label below
            self._text(ctx, label, seg_cx, cy + 24, size=11,
                       align='center', color=self.GRAY)

        self._divider(ctx, y0 + h - 1)
        return y0 + h

    # ── Column headers ────────────────────────────────────────────────────────

    def _draw_col_headers(self, ctx, data: BatchCardData, y0: int) -> int:
        h      = 36
        ty     = y0 + 15
        centers = self._col_centers()

        self._text(ctx, "Маршрут",
                   self.H_PAD + 20, ty, size=13, color=self.GRAY)

        for cx, label in zip(centers, ["Сегодня", "Завтра", data.col3_label, data.col4_label]):
            self._text(ctx, label, cx, ty, size=13, align='center', color=self.GRAY)

        self._divider(ctx, y0 + h - 1)
        return y0 + h

    # ── Route rows ────────────────────────────────────────────────────────────

    def _draw_rows(self, ctx, data: BatchCardData, y0: int) -> int:
        centers = self._col_centers()
        bar_w   = 72
        bar_h   = 10

        for i, route in enumerate(data.routes):
            ry    = y0 + i * self.ROW_H
            mid_y = ry + self.ROW_H / 2

            if i > 0:
                self._divider(ctx, ry)

            # Verdict dot
            ctx.set_source_rgb(*self._level_color(route.today_level))
            ctx.arc(self.H_PAD + 10, mid_y, 6, 0, 2 * math.pi)
            ctx.fill()

            # Route name
            name = route.name
            if len(name) > 18:
                name = name[:17] + "…"
            self._text(ctx, name, self.H_PAD + 22, mid_y + 6, size=14, bold=True)

            # Mini bars (fill = 100 - ci, so fuller = better)
            days = [
                (route.today_ci,    route.today_level),
                (route.tomorrow_ci, route.tomorrow_level),
                (route.saturday_ci, route.saturday_level),
                (route.sunday_ci,   route.sunday_level),
            ]
            for cx, (ci, level) in zip(centers, days):
                bx     = cx - bar_w / 2
                by     = mid_y - bar_h / 2
                fill_w = max(bar_h, bar_w * (100 - ci) / 100)

                ctx.set_source_rgb(0.12, 0.12, 0.12)
                self._rounded_rect(ctx, bx, by, bar_w, bar_h, bar_h / 2)
                ctx.fill()

                ctx.set_source_rgb(*self._level_color(level))
                self._rounded_rect(ctx, bx, by, fill_w, bar_h, bar_h / 2)
                ctx.fill()

        return y0 + len(data.routes) * self.ROW_H

    # ── Drawing helpers ───────────────────────────────────────────────────────

    def _divider(self, ctx, y: float):
        ctx.set_source_rgb(*self.DIVIDER)
        ctx.rectangle(self.H_PAD, y, self.WIDTH - self.H_PAD * 2, 1)
        ctx.fill()

    def _rounded_rect(self, ctx, x: float, y: float,
                      w: float, h: float, r: float):
        ctx.new_sub_path()
        ctx.arc(x + w - r, y + r,     r, -math.pi / 2, 0)
        ctx.arc(x + w - r, y + h - r, r,  0,            math.pi / 2)
        ctx.arc(x + r,     y + h - r, r,  math.pi / 2,  math.pi)
        ctx.arc(x + r,     y + r,     r,  math.pi,      3 * math.pi / 2)
        ctx.close_path()

    def _text(self, ctx, text: str, x: float, y: float,
              size: int = 16,
              bold: bool = False,
              align: str = 'left',
              color: Optional[Color] = None):
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
