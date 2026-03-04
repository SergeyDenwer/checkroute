#!/usr/bin/env python3
"""
CheckRoute — Telegram бот для проверки состояния маршрутов
Загрузи GPX файл — узнай, можно ли сейчас катать и когда высохнет.

Использование:
  1. Создай бота через @BotFather, получи токен
  2. export TELEGRAM_BOT_TOKEN="твой_токен"
  3. python trail_bot.py
"""

import os
import logging
import tempfile
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# Импортируем логику из v4
from trail_moisture_v4 import (
    parse_gpx,
    sample_points_by_distance,
    get_soil_params,
    fetch_weather_data,
    simulate_moisture,
    get_status,
    get_trail_verdict,
    aggregate_status,
    forecast_trail_drying,
    haversine_distance,
    SOIL_PARAMS_TABLE,
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
ROUTES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "routes")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    await update.message.reply_text(
        "🛤 <b>CheckRoute</b>\n\n"
        "Проверю твой маршрут — скажу, можно ли катать "
        "и когда высохнет.\n\n"
        "<b>Как использовать:</b>\n"
        "Просто отправь GPX файл\n\n"
        "<b>Команды:</b>\n"
        "/soil — выбрать тип почвы\n"
        "/batch — сводка по всем маршрутам\n"
        "/help — справка\n\n"
        f"Тип почвы: <b>{SOIL_PARAMS_TABLE[DEFAULT_SOIL]['name']}</b>",
        parse_mode='HTML'
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help"""
    soil_list = "\n".join([f"  <code>{k}</code> — {v['name']}" for k, v in SOIL_PARAMS_TABLE.items()])

    await update.message.reply_text(
        "🛤 <b>CheckRoute — Справка</b>\n\n"
        "<b>Как использовать:</b>\n"
        "Отправь GPX файл → получи отчёт\n"
        "/batch — сводка по популярным маршрутам "
        "(сейчас · завтра · суббота)\n\n"
        "<b>Типы почвы:</b>\n"
        f"{soil_list}\n\n"
        "<b>Выбрать почву:</b>\n"
        "<code>/soil chernozem</code>\n\n"
        "<b>Статусы:</b>\n"
        "☀️ СУХО — отлично\n"
        "🟠 ВЛАЖНО — скользко\n"
        "🔴 ГРЯЗЬ — грязно\n"
        "💀 МЕСИВО — жопа\n\n"
        "<b>Вердикты:</b>\n"
        "✅ МОЖНО\n"
        "🟢 СКОРЕЕ МОЖНО\n"
        "🟠 СКОРЕЕ НЕЛЬЗЯ\n"
        "🔴 НЕЛЬЗЯ",
        parse_mode='HTML'
    )


async def soil_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /soil — выбор типа почвы"""
    if context.args and context.args[0] in SOIL_PARAMS_TABLE:
        soil_type = context.args[0]
        context.user_data['soil'] = soil_type
        params = SOIL_PARAMS_TABLE[soil_type]
        await update.message.reply_text(
            f"✅ Тип почвы изменён на: <b>{params['name']}</b>\n"
            f"Desorptivity: {params['desorptivity']} мм/√день\n"
            f"Ёмкость: {params['capacity']} мм",
            parse_mode='HTML'
        )
    else:
        soil_list = ", ".join([f"<code>{k}</code>" for k in SOIL_PARAMS_TABLE.keys()])
        current = context.user_data.get('soil', DEFAULT_SOIL)
        await update.message.reply_text(
            f"Текущий тип почвы: <b>{SOIL_PARAMS_TABLE[current]['name']}</b>\n\n"
            f"Доступные типы:\n{soil_list}\n\n"
            f"Пример: <code>/soil chernozem</code>",
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

        soil_type = context.user_data.get('soil', DEFAULT_SOIL)
        report = await analyze_gpx(gpx_path, soil_type, status_msg)

        # Отправляем отчёт
        await status_msg.edit_text(report, parse_mode='HTML')

    except Exception as e:
        logger.error(f"Error processing GPX: {e}")
        await message.reply_text("❌ Ошибка обработки файла")
    finally:
        if gpx_path and os.path.exists(gpx_path):
            os.unlink(gpx_path)


async def analyze_gpx(gpx_path: str, soil_type: str, message) -> str:
    """Анализ GPX и формирование отчёта"""
    
    # Парсим GPX
    points = parse_gpx(gpx_path)
    if not points:
        return "❌ GPX файл пустой или повреждён"
    
    # Считаем длину
    total_distance = 0
    for i in range(1, len(points)):
        total_distance += haversine_distance(
            points[i-1][0], points[i-1][1],
            points[i][0], points[i][1]
        )
    
    # Сэмплируем точки
    sampled = sample_points_by_distance(points, DEFAULT_SAMPLE_KM)
    
    soil_params = get_soil_params(soil_type)
    
    # Анализ каждой точки
    results = []
    errors = 0
    
    await message.edit_text(
        f"📍 Точек: {len(points)}, длина: {total_distance:.1f} км\n"
        f"🔬 Анализирую {len(sampled)} контрольных точек..."
    )
    
    for idx, (lat, lon, elev, dist_km) in enumerate(sampled):
        try:
            weather = fetch_weather_data(lat, lon, days_back=14)
            state = simulate_moisture(weather, soil_params)
            status_label, status_key = get_status(state["moisture"], state["capacity"])
            
            results.append({
                "lat": lat,
                "lon": lon,
                "elevation": elev,
                "distance_km": dist_km,
                "moisture": state["moisture"],
                "capacity": state["capacity"],
                "days_dry": state["days_dry"],
                "snow_cover": state["snow_cover"],
                "status_label": status_label,
                "status_key": status_key,
            })
        except Exception as e:
            errors += 1
            logger.warning(f"Point {idx} error: {e}")
    
    if not results:
        return "❌ Не удалось получить данные погоды ни для одной точки"
    
    # Агрегируем статусы
    agg = aggregate_status(results)
    
    dry_pct = agg.get("dry", {}).get("percent", 0)
    wet_pct = agg.get("wet", {}).get("percent", 0)
    mud_pct = agg.get("mud", {}).get("percent", 0)
    swamp_pct = agg.get("swamp", {}).get("percent", 0)
    
    verdict, _ = get_trail_verdict(dry_pct, wet_pct, mud_pct, swamp_pct)
    
    # Формируем отчёт
    report = []
    report.append("<b>🛤 CheckRoute</b>")
    report.append(f"📏 {total_distance:.1f} км | 🌍 {soil_params['name']}")
    report.append("")

    # Распределение
    report.append("<b>📊 СОСТОЯНИЕ:</b>")

    def make_bar(pct, width=10):
        filled = int(pct / 100 * width)
        return "█" * filled + "░" * (width - filled)

    for key in ["dry", "wet", "mud", "swamp"]:
        if key in agg:
            info = agg[key]
            bar = make_bar(info["percent"])
            report.append(f"{info['label']} <code>{bar}</code> {info['percent']:.0f}%")

    report.append("")
    report.append(f"<b>🎯 {verdict}</b>")

    # Прогноз
    forecast_info = forecast_trail_drying(results, soil_params, max_forecast_points=10, verbose=False)

    if forecast_info and forecast_info.get("daily_stats"):
        report.append("")
        report.append("<b>🔮 ПРОГНОЗ:</b>")

        # Таблица на 7 дней
        table = ["Дата    Сухо Влаж Гряз Меси"]
        for ds in forecast_info["daily_stats"][:7]:
            date_short = ds["date"][8:10] + "." + ds["date"][5:7]  # DD.MM
            table.append(
                f"{date_short}  {ds['dry_pct']:>3.0f}% {ds['wet_pct']:>3.0f}% "
                f"{ds['mud_pct']:>3.0f}% {ds['swamp_pct']:>3.0f}%"
            )
        for line in table:
            report.append(f"<code>{line}</code>")

        # Переходы с живыми датами
        report.append("")
        seen_levels = set()
        transitions = []

        for ds in forecast_info["daily_stats"]:
            v, level = get_trail_verdict(ds["dry_pct"], ds["wet_pct"], ds["mud_pct"], ds["swamp_pct"])
            if level not in seen_levels:
                transitions.append((ds["date"], v, level))
                seen_levels.add(level)

        transitions.sort(key=lambda x: x[2])

        today = datetime.now().date()
        for date_str, v, level in transitions:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            days_until = (dt.date() - today).days
            if days_until == 0:
                report.append(f"{v}: сегодня")
            else:
                unix_ts = int(dt.replace(hour=12).timestamp())
                date_fmt = date_str[8:10] + "." + date_str[5:7]
                report.append(
                    f"{v}: {date_fmt}"
                    f" (<tg-time unix=\"{unix_ts}\" format=\"r\">через {days_until} дн</tg-time>)"
                )

    if errors > 0:
        report.append("")
        report.append(f"⚠️ <i>{errors} точек пропущено из-за ошибок API</i>")

    return "\n".join(report)


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
            weather = fetch_weather_data(lat, lon, days_back=14)
            state = simulate_moisture(weather, soil_params)
            status_label, status_key = get_status(state["moisture"], state["capacity"])
            results.append({
                "lat": lat, "lon": lon, "elevation": elev,
                "distance_km": dist_km,
                "moisture": state["moisture"],
                "capacity": state["capacity"],
                "days_dry": state["days_dry"],
                "snow_cover": state["snow_cover"],
                "status_label": status_label,
                "status_key": status_key,
            })
        except Exception:
            pass

    if not results:
        return None

    # Агрегация текущего состояния
    agg = aggregate_status(results)
    today_dry = agg.get("dry", {}).get("percent", 0)
    today_wet = agg.get("wet", {}).get("percent", 0)
    today_mud = agg.get("mud", {}).get("percent", 0)
    today_swamp = agg.get("swamp", {}).get("percent", 0)
    _, today_level = get_trail_verdict(today_dry, today_wet, today_mud, today_swamp)

    # Прогноз — берём меньше точек чтобы не долбить API
    tomorrow_dry = today_dry
    saturday_dry = today_dry
    forecast_info = forecast_trail_drying(results, soil_params, max_forecast_points=5, verbose=False)
    if forecast_info and forecast_info.get("daily_stats"):
        for ds in forecast_info["daily_stats"]:
            ds_date = datetime.strptime(ds["date"], "%Y-%m-%d").date()
            if ds_date == tomorrow:
                tomorrow_dry = ds["dry_pct"]
            if ds_date == saturday:
                saturday_dry = ds["dry_pct"]

    return {
        "today_dry": today_dry,
        "today_level": today_level,
        "tomorrow_dry": tomorrow_dry,
        "saturday_dry": saturday_dry,
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

    soil_type = context.user_data.get('soil', DEFAULT_SOIL)
    soil_params = get_soil_params(soil_type)

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

    # Сортируем: лучшие сверху
    route_results.sort(key=lambda r: r["today_dry"], reverse=True)

    # Формируем отчёт в виде таблицы
    sat_label = f"Сб {saturday.strftime('%d.%m')}"
    sat_short = saturday.strftime('%d.%m')

    verdict_emoji = {4: "✅", 3: "🟢", 2: "🟠", 1: "🔴"}
    counts = {4: 0, 3: 0, 2: 0, 1: 0}

    MAX_NAME = 16
    sep = "─" * 34
    # Заголовок: 3 пробела вместо «emoji » чтобы колонки совпадали
    hdr = f"   {'Маршрут':<{MAX_NAME}}  Сч   Зв   {sat_short}"

    table_rows = [hdr, sep]
    for r in route_results:
        e = verdict_emoji.get(r["today_level"], "❓")
        counts[r["today_level"]] += 1
        name = r['name']
        if len(name) > MAX_NAME:
            name = name[:MAX_NAME - 1] + "…"
        row = (
            f"{e} {name:<{MAX_NAME}}"
            f"  {r['today_dry']:>3.0f}%"
            f"  {r['tomorrow_dry']:>3.0f}%"
            f"  {r['saturday_dry']:>3.0f}%"
        )
        table_rows.append(row)
    table_rows.append(sep)

    header = (
        f"<b>📊 Сводка по {len(route_results)} маршрутам</b>\n"
        f"🌍 {soil_params['name']} | 📅 {today.strftime('%d.%m.%Y')}"
    )
    table_lines = "\n".join(f"<code>{row}</code>" for row in table_rows)
    summary = f"✅ {counts[4]} | 🟢 {counts[3]} | 🟠 {counts[2]} | 🔴 {counts[1]} <i>(сегодня)</i>"

    message = f"{header}\n\n{table_lines}\n\n{summary}"

    # Inline-кнопки для перехода к детальному анализу маршрута
    kbd_buttons = []
    row_buf = []
    for r in route_results:
        e = verdict_emoji.get(r["today_level"], "❓")
        label = f"{e} {r['name'][:18]}"
        # callback_data ограничен 64 байтами; префикс "r:" + имя файла
        row_buf.append(InlineKeyboardButton(label, callback_data=f"r:{r['gpx_file'][:61]}"))
        if len(row_buf) == 2:
            kbd_buttons.append(row_buf)
            row_buf = []
    if row_buf:
        kbd_buttons.append(row_buf)

    reply_markup = InlineKeyboardMarkup(kbd_buttons)
    await status_msg.edit_text(message, parse_mode='HTML', reply_markup=reply_markup)


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

    soil_type = context.user_data.get('soil', DEFAULT_SOIL)
    report = await analyze_gpx(gpx_path, soil_type, status_msg)
    await status_msg.edit_text(report, parse_mode='HTML')


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
    app.add_handler(CommandHandler("soil", soil_command))
    app.add_handler(CommandHandler("batch", batch_command))
    app.add_handler(CallbackQueryHandler(route_detail_callback, pattern="^r:"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_gpx))
    
    # Запускаем
    print("✅ CheckRoute запущен. Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
