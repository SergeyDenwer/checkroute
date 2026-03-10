#!/usr/bin/env python3
"""
CheckRoute — Telegram бот для проверки состояния маршрутов
Загрузи GPX файл — узнай, можно ли сейчас катать и когда высохнет.

Использование:
  1. Создай бота через @BotFather, получи токен
  2. export TELEGRAM_BOT_TOKEN="твой_токен"
  3. python trail_bot.py
"""

import json
import os
import logging
import tempfile
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

from route_card import (
    RouteCardRenderer, RouteCardData, ForecastRow,
    compute_condition_index, verdict_from_ci, SOIL_DISPLAY,
    BatchCardRenderer, BatchCardData, BatchRouteRow,
)

# Импортируем логику из v4
from trail_moisture_v4 import (
    parse_gpx,
    sample_points_by_distance,

    fetch_weather_data,
    fetch_surface_type,
    apply_surface_modifiers,
    simulate_moisture,
    get_status,
    aggregate_status,
    forecast_trail_drying,
    haversine_distance,
    SOIL_PARAMS_TABLE,
    PAVED_SURFACES,
    get_point_at_distance,
)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Дефолтные настройки
DEFAULT_SAMPLE_KM = 5.0
DEFAULT_SOIL = "loam"
ROUTES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "routes_test")

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
        card_data, error = await analyze_gpx(gpx_path, DEFAULT_SOIL, status_msg, route_name)

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


async def analyze_gpx(gpx_path: str, soil_type: str, message, route_name: str = ""):
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

    sampled = sample_points_by_distance(points, DEFAULT_SAMPLE_KM)
    soil_params = SOIL_PARAMS_TABLE[DEFAULT_SOIL].copy()

    await message.edit_text(
        f"📍 Точек: {len(points)}, длина: {total_distance:.1f} км\n"
        f"🔬 Анализирую {len(sampled)} контрольных точек..."
    )

    results = []
    errors = 0
    for idx, (lat, lon, elev, dist_km) in enumerate(sampled):
        try:
            surface = fetch_surface_type(lat, lon)
            if surface in PAVED_SURFACES:
                # Пробуем точку +1 км по треку
                shifted = get_point_at_distance(points, dist_km + 1.0)
                if shifted is None:
                    continue  # конец трека — пропускаем
                lat, lon, elev, dist_km = shifted
                surface = fetch_surface_type(lat, lon)
                if surface in PAVED_SURFACES:
                    continue  # снова асфальт — пропускаем
            weather = fetch_weather_data(lat, lon, days_back=14)
            point_soil = apply_surface_modifiers(soil_params, surface)
            state = simulate_moisture(weather, point_soil)
            status_label, status_key = get_status(state["moisture"], state["capacity"])
            results.append({
                "lat": lat, "lon": lon, "elevation": elev,
                "distance_km": dist_km,
                "moisture": state["moisture"],
                "capacity": state["capacity"],
                "wet_index": state["wet_index"],
                "snow_cover": state["snow_cover"],
                "surface": surface,
                "status_label": status_label,
                "status_key": status_key,
            })
        except Exception as e:
            errors += 1
            logger.warning(f"Point {idx} error: {e}")

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
    forecast_info = forecast_trail_drying(results, soil_params, max_forecast_points=10, verbose=False)

    if forecast_info and forecast_info.get("daily_stats"):
        today = datetime.now().date()
        seen_levels = set()
        transitions = []

        for ds in forecast_info["daily_stats"]:
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
        soil_name=SOIL_DISPLAY.get(soil_type, soil_params["name"]),
        condition_index=ci,
        verdict_text=VERDICT_LABELS[verdict_level],
        verdict_level=verdict_level,
        dry_pct=dry_pct,
        wet_pct=wet_pct,
        mud_pct=mud_pct,
        swamp_pct=swamp_pct,
        forecast_rows=forecast_rows,
    )

    return card_data, None


def analyze_route_for_batch(gpx_path, soil_params, tomorrow, saturday):
    """Анализ одного маршрута для сводки. Возвращает dict или None."""
    points = parse_gpx(gpx_path)
    if not points:
        return None

    sampled = sample_points_by_distance(points, DEFAULT_SAMPLE_KM)

    # Текущее состояние по каждой точке
    results = []
    for lat, lon, elev, dist_km in sampled:
        try:
            surface = fetch_surface_type(lat, lon)
            if surface in PAVED_SURFACES:
                shifted = get_point_at_distance(points, dist_km + 1.0)
                if shifted is None:
                    continue
                lat, lon, elev, dist_km = shifted
                surface = fetch_surface_type(lat, lon)
                if surface in PAVED_SURFACES:
                    continue
            weather = fetch_weather_data(lat, lon, days_back=14)
            point_soil = apply_surface_modifiers(soil_params, surface)
            state = simulate_moisture(weather, point_soil)
            status_label, status_key = get_status(state["moisture"], state["capacity"])
            results.append({
                "lat": lat, "lon": lon, "elevation": elev,
                "distance_km": dist_km,
                "moisture": state["moisture"],
                "capacity": state["capacity"],
                "wet_index": state["wet_index"],
                "snow_cover": state["snow_cover"],
                "surface": surface,
                "status_label": status_label,
                "status_key": status_key,
            })
        except Exception:
            pass

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
    forecast_info = forecast_trail_drying(results, soil_params, max_forecast_points=5, verbose=False)
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

    return {
        "today_ci": today_ci,
        "today_level": today_level,
        "tomorrow_ci": tomorrow_ci,
        "tomorrow_level": tomorrow_level,
        "saturday_ci": saturday_ci,
        "saturday_level": saturday_level,
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

    soil_params = SOIL_PARAMS_TABLE[DEFAULT_SOIL].copy()

    # Целевые даты
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    days_to_sat = (5 - today.weekday()) % 7
    # Если суббота <= завтра — берём следующую, чтобы все 3 колонки были разными
    saturday = today + timedelta(days=days_to_sat if days_to_sat > 1 else days_to_sat + 7)

    status_msg = await update.message.reply_text(
        f"📊 Анализирую {len(gpx_files)} маршрутов...\n"
        f"🌍 {soil_params['name']}\n\n"
        f"Это займёт несколько минут ☕"
    )

    route_results = []

    for file_idx, gpx_file in enumerate(gpx_files):
        route_name = os.path.splitext(gpx_file)[0].replace('_', ' ')
        gpx_path = os.path.join(ROUTES_DIR, gpx_file)

        try:
            await status_msg.edit_text(
                f"📊 Анализирую ({file_idx + 1}/{len(gpx_files)})...\n"
                f"🔍 {route_name}"
            )
        except Exception:
            pass

        try:
            result = analyze_route_for_batch(gpx_path, soil_params, tomorrow, saturday)
            if result:
                result["name"] = route_name
                result["gpx_file"] = gpx_file
                route_results.append(result)
        except Exception as e:
            logger.error(f"Batch error for {gpx_file}: {e}")

    if not route_results:
        await status_msg.edit_text("❌ Не удалось проанализировать ни одного маршрута")
        return

    # Сортируем: лучшие сверху (выше level = лучше, ниже ci = лучше)
    route_results.sort(key=lambda r: (-r["today_level"], r["today_ci"]))

    sat_label = f"Сб {saturday.strftime('%d.%m')}"

    # Строим данные для картинки
    batch_data = BatchCardData(
        soil_name  = soil_params['name'],
        date_str   = today.strftime('%d.%m.%Y'),
        col3_label = sat_label,
        routes     = [
            BatchRouteRow(
                name           = r["name"],
                today_ci       = r["today_ci"],
                today_level    = r["today_level"],
                tomorrow_ci    = r["tomorrow_ci"],
                tomorrow_level = r["tomorrow_level"],
                saturday_ci    = r["saturday_ci"],
                saturday_level = r["saturday_level"],
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

    card_data, error = await analyze_gpx(gpx_path, DEFAULT_SOIL, status_msg, route_name)

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
