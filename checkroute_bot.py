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
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Импортируем логику из v4
from trail_moisture_v4 import (
    parse_gpx,
    sample_points_by_distance,
    get_soil_params,
    fetch_weather_data,
    fetch_forecast,
    simulate_moisture,
    simulate_forecast,
    get_status,
    get_trail_verdict,
    aggregate_status,
    forecast_trail_drying,
    haversine_distance,
    STATUS_THRESHOLDS,
    SOIL_PARAMS_TABLE,
    RAIN_THRESHOLD,
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    await update.message.reply_text(
        "🛤 *CheckRoute*\n\n"
        "Проверю твой маршрут — скажу, можно ли катать "
        "и когда высохнет.\n\n"
        "*Как использовать:*\n"
        "Просто отправь GPX файл\n\n"
        "*Команды:*\n"
        "/soil — выбрать тип почвы\n"
        "/help — справка\n\n"
        f"Тип почвы: *{SOIL_PARAMS_TABLE[DEFAULT_SOIL]['name']}*",
        parse_mode='Markdown'
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help"""
    soil_list = "\n".join([f"  `{k}` — {v['name']}" for k, v in SOIL_PARAMS_TABLE.items()])
    
    await update.message.reply_text(
        "🛤 *CheckRoute — Справка*\n\n"
        "*Как использовать:*\n"
        "Отправь GPX файл → получи отчёт\n\n"
        "*Типы почвы:*\n"
        f"{soil_list}\n\n"
        "*Выбрать почву:*\n"
        "`/soil chernozem`\n\n"
        "*Статусы:*\n"
        "☀️ СУХО — отлично\n"
        "🟠 ВЛАЖНО — скользко\n"
        "🔴 ГРЯЗЬ — грязно\n"
        "💀 МЕСИВО — жопа\n\n"
        "*Вердикты:*\n"
        "✅ МОЖНО\n"
        "🟢 СКОРЕЕ МОЖНО\n"
        "🟠 СКОРЕЕ НЕЛЬЗЯ\n"
        "🔴 НЕЛЬЗЯ",
        parse_mode='Markdown'
    )


async def soil_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /soil — выбор типа почвы"""
    if context.args and context.args[0] in SOIL_PARAMS_TABLE:
        soil_type = context.args[0]
        context.user_data['soil'] = soil_type
        params = SOIL_PARAMS_TABLE[soil_type]
        await update.message.reply_text(
            f"✅ Тип почвы изменён на: *{params['name']}*\n"
            f"Desorptivity: {params['desorptivity']} мм/√день\n"
            f"Ёмкость: {params['capacity']} мм",
            parse_mode='Markdown'
        )
    else:
        soil_list = ", ".join([f"`{k}`" for k in SOIL_PARAMS_TABLE.keys()])
        current = context.user_data.get('soil', DEFAULT_SOIL)
        await update.message.reply_text(
            f"Текущий тип почвы: *{SOIL_PARAMS_TABLE[current]['name']}*\n\n"
            f"Доступные типы:\n{soil_list}\n\n"
            f"Пример: `/soil chernozem`",
            parse_mode='Markdown'
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
    await message.reply_text("⏳ Загружаю файл...")
    
    try:
        file = await context.bot.get_file(document.file_id)
        
        with tempfile.NamedTemporaryFile(suffix='.gpx', delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            gpx_path = tmp.name
        
        # Анализируем
        await message.reply_text("🔍 Анализирую маршрут...")
        
        soil_type = context.user_data.get('soil', DEFAULT_SOIL)
        report = await analyze_gpx(gpx_path, soil_type, message)
        
        # Удаляем временный файл
        os.unlink(gpx_path)
        
        # Отправляем отчёт
        await message.reply_text(report, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error processing GPX: {e}")
        await message.reply_text(f"❌ Ошибка обработки файла: {e}")


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
    
    await message.reply_text(
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
    
    verdict, comment, _ = get_trail_verdict(dry_pct, wet_pct, mud_pct, swamp_pct)
    
    # Формируем отчёт
    report = []
    report.append(f"*🛤 CheckRoute*")
    report.append(f"📏 {total_distance:.1f} км | 🌍 {soil_params['name']}")
    report.append("")
    
    # Распределение
    report.append("*📊 СОСТОЯНИЕ:*")
    
    def make_bar(pct, width=20):
        filled = int(pct / 100 * width)
        return "█" * filled + "░" * (width - filled)
    
    for key in ["dry", "wet", "mud", "swamp"]:
        if key in agg:
            info = agg[key]
            bar = make_bar(info["percent"])
            report.append(f"{info['label']} `{bar}` {info['percent']:.0f}%")
    
    report.append("")
    report.append(f"*🎯 {verdict}*")
    report.append(f"_{comment}_")
    
    # Прогноз
    forecast_info = forecast_trail_drying(results, soil_params, max_forecast_points=10, verbose=False)
    
    if forecast_info and forecast_info.get("daily_stats"):
        report.append("")
        report.append("*🔮 ПРОГНОЗ:*")
        
        # Таблица на 7 дней
        report.append("```")
        report.append("Дата    Сухо Влаж Гряз Меси")
        for ds in forecast_info["daily_stats"][:7]:
            date_short = ds["date"][5:10]  # MM-DD
            report.append(
                f"{date_short}  {ds['dry_pct']:>3.0f}% {ds['wet_pct']:>3.0f}% "
                f"{ds['mud_pct']:>3.0f}% {ds['swamp_pct']:>3.0f}%"
            )
        report.append("```")
        
        # Переходы
        report.append("")
        prev_verdict = None
        seen_levels = set()
        transitions = []
        
        for ds in forecast_info["daily_stats"]:
            v, _, level = get_trail_verdict(ds["dry_pct"], ds["wet_pct"], ds["mud_pct"], ds["swamp_pct"])
            if level not in seen_levels:
                transitions.append((ds["date"], v, level))
                seen_levels.add(level)
        
        transitions.sort(key=lambda x: x[2])
        
        today = datetime.now().date()
        for date_str, v, level in transitions:
            dt = datetime.strptime(date_str, "%Y-%m-%d").date()
            days_until = (dt - today).days
            if days_until > 0:
                report.append(f"{v}: {date_str} (через {days_until} дн)")
            elif days_until == 0:
                report.append(f"{v}: сегодня")
    
    if errors > 0:
        report.append("")
        report.append(f"⚠️ _{errors} точек пропущено из-за ошибок API_")
    
    return "\n".join(report)


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
    app.add_handler(MessageHandler(filters.Document.ALL, handle_gpx))
    
    # Запускаем
    print("✅ CheckRoute запущен. Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
