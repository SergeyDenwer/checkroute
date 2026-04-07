#!/usr/bin/env python3
"""
CheckRoute — Telegram бот для проверки состояния маршрутов
Загрузи GPX файл — узнай, можно ли сейчас катать и когда высохнет.

Использование:
  1. Создай бота через @BotFather, получи токен
  2. export TELEGRAM_BOT_TOKEN="твой_токен"
  3. python trail_bot.py
"""

import asyncio
import json
import math
import os
import logging
import logging.handlers
import tempfile
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

from route_card import (
    RouteCardRenderer, RouteCardData, ForecastRow,
    compute_condition_index, verdict_from_ci,
    BatchCardRenderer, BatchCardData, BatchRouteRow,
)

# Импортируем логику из v4
from trail_moisture_v4 import (
    parse_gpx,
    sample_points_by_distance,
    compute_point_slopes,
    fetch_weather_data,
    fetch_terrain_info,
    fetch_terrain_info_bulk,
    apply_surface_modifiers,
    apply_forest_modifiers,
    apply_slope_modifier,
    simulate_moisture,
    get_status,
    aggregate_status,
    adaptive_sample_km,
    forecast_trail_drying,
    haversine_distance,
    SOIL_PARAMS,
    PAVED_SURFACES,
)

# Настройка логирования
_LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
_log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")

logging.basicConfig(
    format=_LOG_FORMAT,
    level=logging.INFO,
    # Только файл — избегаем дублей когда stdout редиректится в тот же файл
    handlers=[
        logging.handlers.RotatingFileHandler(
            _log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        ),
    ],
    force=True,  # перезаписываем хендлеры если telegram/httpx уже что-то добавили
)

# trail_moisture_v4 пишет DEBUG-детали по факторам радиации/ветра — включаем
logging.getLogger("trail_moisture_v4").setLevel(logging.DEBUG)

logger = logging.getLogger(__name__)

ROUTES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "routes")

VERDICT_LABELS = {
    0: "ДОЖДЬ",
    1: "НЕЛЬЗЯ",
    2: "СКОРЕЕ НЕЛЬЗЯ",
    3: "СКОРЕЕ МОЖНО",
    4: "МОЖНО",
}

# Порог осадков для статуса ДОЖДЬ в прогнозе (мм, среднее по точкам маршрута)
RAIN_DAY_MM = float(os.getenv("RAIN_DAY_MM", "3"))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    await update.message.reply_text(
        "🛤 <b>CheckRoute</b>\n\n"
        "Проверю твой маршрут — скажу, можно ли катать "
        "и когда высохнет.\n\n"
        "<b>Как использовать:</b>\n"
        "Просто отправь GPX файл\n\n"
        "<b>Команды:</b>\n"
        "/batch — сводка по всем маршрутам\n"
        "/help — справка",
        parse_mode='HTML'
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help"""
    await update.message.reply_text(
        "🛤 <b>CheckRoute — Справка</b>\n\n"
        "<b>Как использовать:</b>\n"
        "Отправь GPX файл → получи отчёт\n"
        "/batch — сводка по всем маршрутам "
        "(сейчас · завтра · суббота)\n\n"
        "<b>Статусы трека:</b>\n"
        "☀️ СУХО — отлично\n"
        "🟠 ВЛАЖНО — скользко\n"
        "🔴 ГРЯЗЬ — грязно\n"
        "💀 МЕСИВО — жопа\n\n"
        "<b>Вердикты:</b>\n"
        "✅ МОЖНО\n"
        "🟢 СКОРЕЕ МОЖНО\n"
        "🟠 СКОРЕЕ НЕЛЬЗЯ\n"
        "🔴 НЕЛЬЗЯ\n"
        "🌧 ДОЖДЬ — в прогнозе осадки",
        parse_mode='HTML'
    )



async def handle_gpx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка GPX файла"""
    message = update.message
    document = message.document
    
    # Проверяем что это GPX
    if not document.file_name.lower().endswith('.gpx'):
        await message.reply_text("❌ Отправь GPX файл (с расширением .gpx)")
        return
    
    # Скачиваем файл
    status_msg = await message.reply_text("⏳ Загружаю файл...")

    gpx_path = None
    try:
        file = await context.bot.get_file(document.file_id)

        with tempfile.NamedTemporaryFile(suffix='.gpx', delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            gpx_path = tmp.name

        # Анализируем
        await status_msg.edit_text("🔍 Анализирую маршрут...")

        route_name = os.path.splitext(document.file_name)[0].replace('_', ' ')
        card_data, error = await analyze_gpx(gpx_path, status_msg, route_name)

        if error:
            await status_msg.edit_text(error, parse_mode='HTML')
        else:
            png = RouteCardRenderer().render(card_data)
            try:
                await status_msg.delete()
            except Exception:
                pass
            await message.reply_photo(photo=png)

    except Exception as e:
        logger.error(f"Error processing GPX: {e}")
        await message.reply_text("❌ Ошибка обработки файла")
    finally:
        if gpx_path and os.path.exists(gpx_path):
            os.unlink(gpx_path)


async def analyze_gpx(gpx_path: str, message, route_name: str = ""):
    """
    Анализ GPX.
    Возвращает (RouteCardData, None) при успехе или (None, error_text) при ошибке.
    """
    points = parse_gpx(gpx_path)
    if not points:
        return None, "❌ GPX файл пустой или повреждён"

    total_distance = sum(
        haversine_distance(points[i-1][0], points[i-1][1], points[i][0], points[i][1])
        for i in range(1, len(points))
    )

    sampled = sample_points_by_distance(points, adaptive_sample_km(total_distance))
    slopes  = compute_point_slopes(points, sampled)

    header = f"📍 Точек: {len(points)}, длина: {total_distance:.1f} км\n"
    total = len(sampled)
    await message.edit_text(
        header + f"🗺 Загружаю карту покрытий ({total} точек)..."
    )

    terrain_list = await asyncio.to_thread(fetch_terrain_info_bulk, sampled)

    await message.edit_text(
        header + f"🔬 Анализирую {total} контрольных точек...\n⏳ 0/{total}"
    )

    # Иконки поверхностей для живого лога
    _SURFACE_ICONS = {
        "asphalt": "🛣", "paved": "🛣", "concrete": "🛣",
        "gravel": "🪨", "fine_gravel": "🪨", "compacted": "🪨",
        "ground": "🌱", "dirt": "🌱", "unpaved": "🌱", "grass": "🌿",
        "sand": "🏖", "mud": "💧", "rock": "⛰",
    }

    def _surface_icon(s: str) -> str:
        return _SURFACE_ICONS.get(s, "❓")

    results = []
    errors = 0
    skipped_paved = 0
    progress_lines: list[str] = []

    for idx, (lat, lon, elev, dist_km) in enumerate(sampled):
        try:
            terrain = terrain_list[idx]
            surface   = terrain["surface"]
            is_forest = terrain["is_forest"]
            leaf_type = terrain["leaf_type"]
            slope_rad = slopes[idx]

            if surface == "error":
                skipped_paved += 1
                progress_lines.append(f"  км {dist_km:.1f} — ⚠️ нет данных OSM")
            elif surface in PAVED_SURFACES:
                skipped_paved += 1
                progress_lines.append(f"  км {dist_km:.1f} — {_surface_icon(surface)} {surface} (пропущено)")
            if surface not in PAVED_SURFACES and surface != "error":
                weather = fetch_weather_data(lat, lon, days_back=14)
                point_soil = apply_surface_modifiers(SOIL_PARAMS, surface)
                point_soil = apply_forest_modifiers(point_soil, is_forest, leaf_type)
                point_soil = apply_slope_modifier(point_soil, slope_rad)
                state = simulate_moisture(weather, point_soil)
                status_label, status_key = get_status(state["moisture"], state["capacity"])
                forest_mark = "🌲" if is_forest else ""
                logger.info(
                    "point km=%.1f surface=%s forest=%s leaf=%s slope=%.1f° "
                    "cap=%.2f desorpt=%.2f snow_f=%.2f rain_f=%.2f "
                    "moisture=%.2f capacity=%.2f snow_cover=%.1f stage2=%d → %s",
                    dist_km, surface, is_forest, leaf_type,
                    math.degrees(slope_rad),
                    point_soil["capacity"], point_soil["desorptivity"],
                    point_soil["snow_factor"], point_soil.get("rain_factor", 1.0),
                    state["moisture"], state["capacity"], state["snow_cover"],
                    state.get("stage2_days", 0), status_key,
                )
                results.append({
                    "lat": lat, "lon": lon, "elevation": elev,
                    "distance_km": dist_km,
                    "moisture":    state["moisture"],
                    "capacity":    state["capacity"],
                    "wet_index":   state["wet_index"],
                    "snow_cover":  state["snow_cover"],
                    "stage2_days": state.get("stage2_days", 0),
                    "surface":     surface,
                    "is_forest":   is_forest,
                    "leaf_type":   leaf_type,
                    "slope_rad":   slope_rad,
                    "status_label": status_label,
                    "status_key":   status_key,
                })
                progress_lines.append(
                    f"  км {dist_km:.1f} — {_surface_icon(surface)} {surface}"
                    f"{forest_mark} → {status_label}"
                )
        except Exception as e:
            errors += 1
            logger.warning(f"Point {idx} error: {e}")
            progress_lines.append(f"  км {dist_km:.1f} — ❌ ошибка")

        done = idx + 1
        bar = "▓" * done + "░" * (total - done)
        log_tail = "\n".join(progress_lines[-5:])  # последние 5 строк чтобы не вылезти за лимит
        try:
            await message.edit_text(
                header
                + f"🔬 Анализирую точку {done}/{total}\n"
                + f"[{bar}]\n"
                + log_tail
            )
        except Exception:
            pass  # TelegramError (flood/not modified) — не критично

    if not results:
        return None, "❌ Не удалось получить данные погоды ни для одной точки"

    agg = aggregate_status(results)
    dry_pct   = agg.get("dry",   {}).get("percent", 0)
    wet_pct   = agg.get("wet",   {}).get("percent", 0)
    mud_pct   = agg.get("mud",   {}).get("percent", 0)
    swamp_pct = agg.get("swamp", {}).get("percent", 0)

    ci = compute_condition_index(dry_pct, wet_pct, mud_pct, swamp_pct)
    _, verdict_level = verdict_from_ci(ci)

    # Строим строки прогноза
    forecast_rows = []
    try:
        await message.edit_text(header + "🔮 Загружаю прогноз погоды (может занять ~минуту)...")
    except Exception:
        pass
    forecast_info = await asyncio.to_thread(forecast_trail_drying, results, False)

    if not forecast_info:
        logger.warning("analyze_gpx: forecast_trail_drying returned None — forecast section will be empty")
    elif not forecast_info.get("daily_stats"):
        logger.warning("analyze_gpx: forecast_info has no daily_stats — forecast section will be empty")

    if forecast_info and forecast_info.get("daily_stats"):
        today = datetime.now().date()
        seen_levels = set()
        transitions = []

        for ds in forecast_info["daily_stats"]:
            ds_date = datetime.strptime(ds["date"], "%Y-%m-%d").date()
            if ds_date == today:
                continue  # сегодня уже показан как текущий статус — не дублируем в прогнозе
            if ds.get("avg_rain", 0) >= RAIN_DAY_MM:
                level = 0  # ДОЖДЬ — симуляция на этот день бессмысленна
            else:
                ds_ci = compute_condition_index(ds["dry_pct"], ds["wet_pct"], ds["mud_pct"], ds["swamp_pct"])
                _, level = verdict_from_ci(ds_ci)
            if level not in seen_levels:
                transitions.append((ds["date"], level))
                seen_levels.add(level)

        transitions.sort(key=lambda x: x[1])  # worst first (0=дождь, 1=нельзя … 4=можно)

        for date_str, level in transitions:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            days_until = (dt.date() - today).days
            if days_until == 0:
                date_label = "сегодня"
            else:
                date_label = f"{date_str[8:10]}.{date_str[5:7]} (через {days_until} дн.)"
            forecast_rows.append(ForecastRow(
                level=level,
                label=VERDICT_LABELS[level],
                date_str=date_label,
            ))

    card_data = RouteCardData(
        route_name=route_name or "Маршрут",
        length_km=round(total_distance, 1),
        condition_index=ci,
        verdict_text=VERDICT_LABELS[verdict_level],
        verdict_level=verdict_level,
        dry_pct=dry_pct,
        wet_pct=wet_pct,
        mud_pct=mud_pct,
        swamp_pct=swamp_pct,
        forecast_rows=forecast_rows,
        points_sampled=len(sampled),
        points_analyzed=len(results),
    )

    return card_data, None


async def analyze_route_for_batch(gpx_path, tomorrow, saturday, sunday, on_progress=None):
    """Анализ одного маршрута для сводки. Возвращает dict или None.
    on_progress(done, total, dist_km, surface, status_label) вызывается после каждой точки.
    """
    points = parse_gpx(gpx_path)
    if not points:
        return None

    total_distance = sum(
        haversine_distance(points[i-1][0], points[i-1][1], points[i][0], points[i][1])
        for i in range(1, len(points))
    )
    sampled = sample_points_by_distance(points, adaptive_sample_km(total_distance))
    slopes  = compute_point_slopes(points, sampled)
    total = len(sampled)

    terrain_list = await asyncio.to_thread(fetch_terrain_info_bulk, sampled)

    # Текущее состояние по каждой точке
    results = []
    for idx, (lat, lon, elev, dist_km) in enumerate(sampled):
        surface = None
        status_label = None
        try:
            terrain = terrain_list[idx]
            surface   = terrain["surface"]
            is_forest = terrain["is_forest"]
            leaf_type = terrain["leaf_type"]
            slope_rad = slopes[idx]

            if surface == "error":
                if on_progress:
                    await on_progress(idx + 1, total, dist_km, "error", None)
                continue
            if surface in PAVED_SURFACES:
                if on_progress:
                    await on_progress(idx + 1, total, dist_km, surface, None)
                continue
            weather = fetch_weather_data(lat, lon, days_back=14)
            point_soil = apply_surface_modifiers(SOIL_PARAMS, surface)
            point_soil = apply_forest_modifiers(point_soil, is_forest, leaf_type)
            point_soil = apply_slope_modifier(point_soil, slope_rad)
            state = simulate_moisture(weather, point_soil)
            status_label, status_key = get_status(state["moisture"], state["capacity"])
            logger.info(
                "batch point km=%.1f surface=%s forest=%s slope=%.1f° "
                "cap=%.2f desorpt=%.2f moisture=%.2f capacity=%.2f → %s",
                dist_km, surface, is_forest, math.degrees(slope_rad),
                point_soil["capacity"], point_soil["desorptivity"],
                state["moisture"], state["capacity"], status_key,
            )
            results.append({
                "lat": lat, "lon": lon, "elevation": elev,
                "distance_km": dist_km,
                "moisture":    state["moisture"],
                "capacity":    state["capacity"],
                "wet_index":   state["wet_index"],
                "snow_cover":  state["snow_cover"],
                "stage2_days": state.get("stage2_days", 0),
                "surface":     surface,
                "is_forest":   is_forest,
                "leaf_type":   leaf_type,
                "slope_rad":   slope_rad,
                "status_label": status_label,
                "status_key":   status_key,
            })
        except Exception:
            pass
        if on_progress:
            await on_progress(idx + 1, total, dist_km, surface, status_label)

    if not results:
        return None

    # Агрегация текущего состояния
    agg = aggregate_status(results)
    today_dry   = agg.get("dry",   {}).get("percent", 0)
    today_wet   = agg.get("wet",   {}).get("percent", 0)
    today_mud   = agg.get("mud",   {}).get("percent", 0)
    today_swamp = agg.get("swamp", {}).get("percent", 0)
    today_ci = compute_condition_index(today_dry, today_wet, today_mud, today_swamp)
    _, today_level = verdict_from_ci(today_ci)

    # Прогноз — берём меньше точек чтобы не долбить API
    tomorrow_ci    = today_ci
    tomorrow_level = today_level
    saturday_ci    = today_ci
    saturday_level = today_level
    sunday_ci      = today_ci
    sunday_level   = today_level
    forecast_info = await asyncio.to_thread(forecast_trail_drying, results, False)
    if not forecast_info:
        logger.warning("analyze_route_for_batch: forecast_trail_drying returned None — using current condition for all days")
    elif not forecast_info.get("daily_stats"):
        logger.warning("analyze_route_for_batch: forecast_info has no daily_stats — using current condition for all days")
    if forecast_info and forecast_info.get("daily_stats"):
        for ds in forecast_info["daily_stats"]:
            ds_date = datetime.strptime(ds["date"], "%Y-%m-%d").date()
            if ds.get("avg_rain", 0) >= RAIN_DAY_MM:
                ds_ci = None
                ds_level = 0
            else:
                ds_ci = compute_condition_index(ds["dry_pct"], ds["wet_pct"], ds["mud_pct"], ds["swamp_pct"])
                _, ds_level = verdict_from_ci(ds_ci)
            if ds_date == tomorrow:
                tomorrow_ci    = ds_ci if ds_ci is not None else 0
                tomorrow_level = ds_level
            if ds_date == saturday:
                saturday_ci    = ds_ci if ds_ci is not None else 0
                saturday_level = ds_level
            if ds_date == sunday:
                sunday_ci      = ds_ci if ds_ci is not None else 0
                sunday_level   = ds_level

    return {
        "today_ci": today_ci,
        "today_level": today_level,
        "tomorrow_ci": tomorrow_ci,
        "tomorrow_level": tomorrow_level,
        "saturday_ci": saturday_ci,
        "saturday_level": saturday_level,
        "sunday_ci": sunday_ci,
        "sunday_level": sunday_level,
    }


async def batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /batch — сводка по всем маршрутам из папки routes/"""
    if not os.path.isdir(ROUTES_DIR):
        await update.message.reply_text("❌ Папка routes/ не найдена")
        return

    gpx_files = sorted(f for f in os.listdir(ROUTES_DIR) if f.lower().endswith('.gpx'))
    if not gpx_files:
        await update.message.reply_text("❌ В папке routes/ нет GPX файлов")
        return

    # Целевые даты
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    days_to_sat = (5 - today.weekday()) % 7
    # Если суббота <= завтра — берём следующую, чтобы все колонки были разными
    saturday = today + timedelta(days=days_to_sat if days_to_sat > 1 else days_to_sat + 7)
    sunday   = saturday + timedelta(days=1)

    status_msg = await update.message.reply_text(
        f"📊 Анализирую {len(gpx_files)} маршрутов...\n"
        f"Это займёт несколько минут ☕"
    )

    route_results = []
    done_lines: list[str] = []
    total_files = len(gpx_files)

    for file_idx, gpx_file in enumerate(gpx_files):
        route_name = os.path.splitext(gpx_file)[0].replace('_', ' ')
        gpx_path = os.path.join(ROUTES_DIR, gpx_file)

        bar = "▓" * (file_idx) + "░" * (total_files - file_idx)
        log_tail = "\n".join(done_lines[-6:])
        try:
            await status_msg.edit_text(
                f"📊 Анализирую ({file_idx + 1}/{total_files})\n"
                f"[{bar}]\n"
                f"🔍 {route_name}...\n"
                + (("\n" + log_tail) if log_tail else "")
            )
        except Exception:
            pass

        _SURFACE_ICONS = {
            "asphalt": "🛣", "paved": "🛣", "concrete": "🛣", "sett": "🛣",
            "gravel": "🪨", "fine_gravel": "🪨", "compacted": "🪨",
            "ground": "🌱", "dirt": "🌱", "unpaved": "🌱", "grass": "🌿",
            "sand": "🏖", "mud": "💧", "rock": "⛰", "error": "⚠️",
        }

        point_lines: list[str] = []

        async def on_point_progress(done, total_pts, dist_km, surface, status_label):
            icon = _SURFACE_ICONS.get(surface or "", "❓")
            if surface in PAVED_SURFACES:
                line = f"    км {dist_km:.1f} — {icon} {surface} (пропущено)"
            elif surface == "error" or surface is None:
                line = f"    км {dist_km:.1f} — ⚠️ нет данных"
            else:
                line = f"    км {dist_km:.1f} — {icon} {surface} → {status_label}"
            point_lines.append(line)
            pt_bar = "▓" * done + "░" * (total_pts - done)
            tail = "\n".join(point_lines[-4:])
            try:
                await status_msg.edit_text(
                    f"📊 Анализирую ({file_idx + 1}/{total_files})\n"
                    f"[{bar}]\n"
                    f"🔍 {route_name} — точка {done}/{total_pts}\n"
                    f"  [{pt_bar}]\n"
                    + tail
                    + (("\n\n" + "\n".join(done_lines[-3:])) if done_lines else "")
                )
            except Exception:
                pass

        try:
            result = await analyze_route_for_batch(
                gpx_path, tomorrow, saturday, sunday, on_progress=on_point_progress
            )
            if result:
                result["name"] = route_name
                result["gpx_file"] = gpx_file
                route_results.append(result)
                verdict_text, _ = verdict_from_ci(result["today_ci"])
                done_lines.append(f"  {route_name} — {verdict_text}")
            else:
                done_lines.append(f"  {route_name} — ⚠️ нет данных")
        except Exception as e:
            logger.error(f"Batch error for {gpx_file}: {e}")
            done_lines.append(f"  {route_name} — ❌ ошибка")

    if not route_results:
        await status_msg.edit_text("❌ Не удалось проанализировать ни одного маршрута")
        return

    # Сортируем: лучшие сверху (выше level = лучше, ниже ci = лучше)
    route_results.sort(key=lambda r: (-r["today_level"], r["today_ci"]))

    sat_label = f"Сб {saturday.strftime('%d.%m')}"
    sun_label = f"Вс {sunday.strftime('%d.%m')}"

    # Строим данные для картинки
    batch_data = BatchCardData(
        date_str   = today.strftime('%d.%m.%Y'),
        col3_label = sat_label,
        col4_label = sun_label,
        routes     = [
            BatchRouteRow(
                name           = r["name"],
                today_ci       = r["today_ci"],
                today_level    = r["today_level"],
                tomorrow_ci    = r["tomorrow_ci"],
                tomorrow_level = r["tomorrow_level"],
                saturday_ci    = r["saturday_ci"],
                saturday_level = r["saturday_level"],
                sunday_ci      = r["sunday_ci"],
                sunday_level   = r["sunday_level"],
            )
            for r in route_results
        ],
    )
    png = BatchCardRenderer().render(batch_data)

    # Inline-кнопки для перехода к детальному анализу маршрута
    verdict_emoji = {4: "✅", 3: "🟢", 2: "🟠", 1: "🔴", 0: "🌧"}

    # Загружаем Komoot-ссылки если есть
    komoot_urls: dict = {}
    routes_json = os.path.join(ROUTES_DIR, "routes.json")
    if os.path.isfile(routes_json):
        try:
            with open(routes_json, encoding="utf-8") as _f:
                komoot_urls = json.load(_f)
        except Exception:
            pass

    # Кнопки: каждый маршрут — отдельная строка [✅ Название] [🌐]
    kbd_buttons = []
    for r in route_results:
        e     = verdict_emoji.get(r["today_level"], "❓")
        label = f"{e} {r['name'][:22]}"
        row   = [InlineKeyboardButton(label, callback_data=f"r:{r['gpx_file'][:61]}")]
        url   = komoot_urls.get(r["gpx_file"], "")
        if url:
            row.append(InlineKeyboardButton("🌐", url=url))
        kbd_buttons.append(row)

    reply_markup = InlineKeyboardMarkup(kbd_buttons)

    try:
        await status_msg.delete()
    except Exception:
        pass
    await update.message.reply_photo(photo=png, reply_markup=reply_markup)


async def route_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажатие на кнопку маршрута из /batch — запускает детальный анализ"""
    query = update.callback_query
    await query.answer()

    gpx_file = query.data[2:]  # убираем префикс "r:"
    gpx_path = os.path.join(ROUTES_DIR, gpx_file)

    if not os.path.isfile(gpx_path):
        await query.message.reply_text("❌ Файл маршрута не найден")
        return

    route_name = os.path.splitext(gpx_file)[0].replace('_', ' ')
    status_msg = await query.message.reply_text(f"🔍 Анализирую {route_name}...")

    card_data, error = await analyze_gpx(gpx_path, status_msg, route_name)

    if error:
        await status_msg.edit_text(error, parse_mode='HTML')
    else:
        png = RouteCardRenderer().render(card_data)
        try:
            await status_msg.delete()
        except Exception:
            pass
        await query.message.reply_photo(photo=png)


def main():
    """Запуск бота"""
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    
    if not token:
        print("❌ Установи переменную окружения TELEGRAM_BOT_TOKEN")
        print("   export TELEGRAM_BOT_TOKEN='твой_токен_от_BotFather'")
        return
    
    print("🛤 Запускаю CheckRoute...")
    
    # Создаём приложение
    app = Application.builder().token(token).build()
    
    # Регистрируем обработчики
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("batch", batch_command))
    app.add_handler(CallbackQueryHandler(route_detail_callback, pattern="^r:"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_gpx))
    
    # Запускаем
    print("✅ CheckRoute запущен. Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
