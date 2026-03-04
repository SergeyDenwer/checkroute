#!/usr/bin/env python3
"""
Trail Moisture Index Calculator v4
С поддержкой GPX треков — анализ множества точек вдоль маршрута.

Использование:
  python trail_moisture_v4.py --gpx trail.gpx
  python trail_moisture_v4.py --gpx trail.gpx --soil chernozem
  python trail_moisture_v4.py --gpx trail.gpx --sample-km 3
"""

import requests
from datetime import datetime, timedelta
import argparse
import math
import gpxpy
from collections import defaultdict

# Порог дождя для сброса счётчика дней
RAIN_THRESHOLD = 3.0  # мм

SOIL_PARAMS_TABLE = {
    "sand": {"name": "Песок (sand)", "desorptivity": 4.5, "capacity": 10.0, "stage1_ratio": 0.5},
    "sandy_loam": {"name": "Супесь (sandy loam)", "desorptivity": 4.0, "capacity": 12.0, "stage1_ratio": 0.5},
    "loam": {"name": "Суглинок (loam)", "desorptivity": 3.5, "capacity": 15.0, "stage1_ratio": 0.5},
    "silt_loam": {"name": "Пылеватый суглинок (silt loam)", "desorptivity": 3.2, "capacity": 16.0, "stage1_ratio": 0.5},
    "clay_loam": {"name": "Глинистый суглинок (clay loam)", "desorptivity": 3.0, "capacity": 18.0, "stage1_ratio": 0.5},
    "clay": {"name": "Глина (clay)", "desorptivity": 2.5, "capacity": 20.0, "stage1_ratio": 0.5},
    "chernozem": {"name": "Чернозём (chernozem)", "desorptivity": 3.0, "capacity": 18.0, "stage1_ratio": 0.5},
}

STATUS_THRESHOLDS = [
    (0.20, "☀️ СУХО", "dry"),
    (0.45, "🟠 ВЛАЖНО", "wet"),
    (0.75, "🔴 ГРЯЗЬ", "mud"),
    (1.00, "💀 МЕСИВО", "swamp"),
]


def haversine_distance(lat1, lon1, lat2, lon2):
    """Расстояние между двумя точками в км (формула Haversine)"""
    R = 6371  # радиус Земли в км
    
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    
    return R * c


def parse_gpx(gpx_file):
    """Парсим GPX файл, возвращаем список точек [(lat, lon, elevation), ...]"""
    with open(gpx_file, 'r') as f:
        gpx = gpxpy.parse(f)
    
    points = []
    for track in gpx.tracks:
        for segment in track.segments:
            for point in segment.points:
                points.append((point.latitude, point.longitude, point.elevation or 0))
    
    # Если нет треков, пробуем маршруты
    if not points:
        for route in gpx.routes:
            for point in route.points:
                points.append((point.latitude, point.longitude, point.elevation or 0))
    
    # Если нет маршрутов, пробуем waypoints
    if not points:
        for point in gpx.waypoints:
            points.append((point.latitude, point.longitude, point.elevation or 0))
    
    return points


def sample_points_by_distance(points, sample_km=5.0):
    """
    Выбираем точки через каждые sample_km километров.
    Возвращаем список (lat, lon, elevation, distance_km).
    """
    if not points:
        return []
    
    sampled = [(points[0][0], points[0][1], points[0][2], 0.0)]
    cumulative_distance = 0.0
    last_sample_distance = 0.0
    
    for i in range(1, len(points)):
        lat1, lon1, _ = points[i-1]
        lat2, lon2, elev2 = points[i]
        
        segment_dist = haversine_distance(lat1, lon1, lat2, lon2)
        cumulative_distance += segment_dist
        
        # Если прошли ещё sample_km от последней точки
        if cumulative_distance - last_sample_distance >= sample_km:
            sampled.append((lat2, lon2, elev2, cumulative_distance))
            last_sample_distance = cumulative_distance
    
    # Добавляем последнюю точку если она не слишком близко к предыдущей
    last_point = points[-1]
    if cumulative_distance - last_sample_distance > sample_km * 0.3:
        sampled.append((last_point[0], last_point[1], last_point[2], cumulative_distance))
    
    return sampled


def get_soil_params(soil_type=None):
    """Возвращает параметры почвы."""
    if soil_type and soil_type in SOIL_PARAMS_TABLE:
        return SOIL_PARAMS_TABLE[soil_type].copy()
    return SOIL_PARAMS_TABLE["loam"].copy()


def fetch_weather_data(lat, lon, days_back=14, target_date=None):
    """Получаем данные погоды из Open-Meteo.

    target_date — дата, за которую смотрим состояние (по умолчанию сегодня).
    """
    base = datetime.strptime(target_date, "%Y-%m-%d") if target_date else datetime.now()
    end_date = base.strftime("%Y-%m-%d")
    start_date = (base - timedelta(days=days_back)).strftime("%Y-%m-%d")
    
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "daily": ",".join([
            "temperature_2m_mean",
            "rain_sum",
            "snowfall_sum",
            "et0_fao_evapotranspiration",
        ]),
        "timezone": "auto"
    }
    
    response = requests.get(url, params=params, timeout=15)
    if response.status_code != 200:
        raise Exception(f"API Error: {response.status_code}")
    return response.json()


def fetch_forecast(lat, lon, days_ahead=16):
    """Получаем прогноз погоды"""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": ",".join([
            "temperature_2m_max",
            "temperature_2m_min",
            "rain_sum",
            "snowfall_sum",
            "et0_fao_evapotranspiration",
        ]),
        "forecast_days": days_ahead,
        "timezone": "auto"
    }
    
    response = requests.get(url, params=params, timeout=15)
    if response.status_code != 200:
        raise Exception(f"Forecast API Error: {response.status_code}")
    return response.json()


def _simulate_day(temp_mean, rain, snowfall_cm, eto, surface_moisture, snow_cover, days_since_rain, soil_params):
    """Один шаг симуляции влажности (один день)."""
    DESORPTIVITY = soil_params["desorptivity"]
    CAPACITY = soil_params["capacity"]
    STAGE1_THRESHOLD = CAPACITY * soil_params["stage1_ratio"]

    snowfall_mm = snowfall_cm * 10
    water_input = rain

    snow_cover += snowfall_mm
    if temp_mean > 0 and snow_cover > 0:
        melt_potential = temp_mean * 3.0
        snow_water = snow_cover * 0.1
        actual_melt = min(snow_water, melt_potential)
        snow_cover -= actual_melt * 10
        snow_cover = max(0, snow_cover)
        water_input += actual_melt

    if water_input >= RAIN_THRESHOLD:
        days_since_rain = 1
    else:
        days_since_rain += 1

    if snow_cover > 5:
        evaporation = 0.05
    elif surface_moisture > STAGE1_THRESHOLD:
        evaporation = eto * 0.9
    else:
        str_factor = math.sqrt(days_since_rain) - math.sqrt(days_since_rain - 1)
        evaporation = DESORPTIVITY * str_factor

    evaporation = min(evaporation, surface_moisture)
    surface_moisture = surface_moisture + water_input - evaporation
    surface_moisture = max(0, min(surface_moisture, CAPACITY))

    return surface_moisture, snow_cover, days_since_rain


def simulate_moisture(weather_data, soil_params):
    """Симуляция влажности поверхностного слоя по историческим данным."""
    daily = weather_data["daily"]
    surface_moisture = 0
    snow_cover = 0
    days_since_rain = 1

    for i in range(len(daily["time"])):
        surface_moisture, snow_cover, days_since_rain = _simulate_day(
            temp_mean=daily["temperature_2m_mean"][i] or 0,
            rain=daily["rain_sum"][i] or 0,
            snowfall_cm=daily["snowfall_sum"][i] or 0,
            eto=daily["et0_fao_evapotranspiration"][i] or 0,
            surface_moisture=surface_moisture,
            snow_cover=snow_cover,
            days_since_rain=days_since_rain,
            soil_params=soil_params,
        )

    return {
        "moisture": surface_moisture,
        "capacity": soil_params["capacity"],
        "days_dry": days_since_rain,
        "snow_cover": snow_cover,
    }


def simulate_forecast(initial_state, forecast_data, soil_params):
    """Симуляция с прогнозом погоды, возвращает результаты по дням."""
    daily = forecast_data["daily"]
    surface_moisture = initial_state["moisture"]
    snow_cover = initial_state.get("snow_cover", 0)
    days_since_rain = initial_state["days_dry"]
    results = []

    for i in range(len(daily["time"])):
        temp_mean = ((daily["temperature_2m_max"][i] or 0) + (daily["temperature_2m_min"][i] or 0)) / 2
        surface_moisture, snow_cover, days_since_rain = _simulate_day(
            temp_mean=temp_mean,
            rain=daily["rain_sum"][i] or 0,
            snowfall_cm=daily["snowfall_sum"][i] or 0,
            eto=daily["et0_fao_evapotranspiration"][i] or 0,
            surface_moisture=surface_moisture,
            snow_cover=snow_cover,
            days_since_rain=days_since_rain,
            soil_params=soil_params,
        )
        results.append({
            "date": daily["time"][i],
            "moisture": surface_moisture,
            "capacity": soil_params["capacity"],
            "rain": daily["rain_sum"][i] or 0,
            "days_dry": days_since_rain,
            "snow_cover": snow_cover,
        })

    return results


def get_status(moisture, capacity):
    """Возвращает статус и ключ"""
    pct = moisture / capacity
    for threshold, label, key in STATUS_THRESHOLDS:
        if pct < threshold:
            return label, key
    return STATUS_THRESHOLDS[-1][1], STATUS_THRESHOLDS[-1][2]


def analyze_trail(gpx_file, soil_params, sample_km=5.0, verbose=True, target_date=None):
    """
    Анализ всего трейла:
    1. Парсим GPX
    2. Сэмплируем точки каждые N км
    3. Для каждой точки получаем погоду и считаем влажность
    4. Агрегируем результаты
    """
    if verbose:
        print(f"📂 Загружаю GPX: {gpx_file}")
    
    points = parse_gpx(gpx_file)
    if not points:
        raise Exception("GPX файл пустой или не содержит точек")
    
    total_distance = 0
    for i in range(1, len(points)):
        total_distance += haversine_distance(
            points[i-1][0], points[i-1][1],
            points[i][0], points[i][1]
        )
    
    if verbose:
        print(f"   Точек в треке: {len(points)}")
        print(f"   Длина трека: {total_distance:.1f} км")
    
    sampled = sample_points_by_distance(points, sample_km)
    if verbose:
        print(f"   Точек для анализа: {len(sampled)} (каждые {sample_km} км)")
        print()
    
    # Анализ каждой точки
    results = []
    for idx, (lat, lon, elev, dist_km) in enumerate(sampled):
        if verbose:
            print(f"   [{idx+1}/{len(sampled)}] км {dist_km:.1f}: ({lat:.4f}, {lon:.4f})...", end=" ", flush=True)
        
        try:
            weather = fetch_weather_data(lat, lon, days_back=14, target_date=target_date)
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
                "weather": weather,
            })
            
            if verbose:
                print(f"{status_label} ({state['moisture']:.1f}мм)")
        
        except Exception as e:
            if verbose:
                print(f"⚠️ Ошибка: {e}")
            results.append({
                "lat": lat, "lon": lon, "elevation": elev, "distance_km": dist_km,
                "error": str(e)
            })
    
    return results, total_distance


def aggregate_status(results):
    """Агрегируем статусы в проценты"""
    counts = defaultdict(int)
    valid = [r for r in results if "status_key" in r]
    
    for r in valid:
        counts[r["status_key"]] += 1
    
    total = len(valid)
    if total == 0:
        return {}
    
    percentages = {}
    for threshold, label, key in STATUS_THRESHOLDS:
        percentages[key] = {
            "label": label,
            "count": counts[key],
            "percent": counts[key] / total * 100
        }
    
    return percentages


def forecast_trail_drying(results, soil_params, max_forecast_points=10, verbose=True):
    """
    Прогноз когда трейл станет сухим.
    Берём равномерно распределённые точки, симулируем высыхание, усредняем.
    """
    valid = [r for r in results if "moisture" in r]
    if not valid:
        return None
    
    # Берём равномерно распределённые точки (максимум max_forecast_points)
    num_points = min(max_forecast_points, len(valid))
    step = max(1, len(valid) // num_points)
    forecast_points = valid[::step][:num_points]  # ограничиваем сверху
    
    if verbose:
        print(f"\n🔮 Прогноз высыхания по {len(forecast_points)} точкам...")
    
    all_forecasts = []
    
    for idx, point in enumerate(forecast_points):
        try:
            forecast = fetch_forecast(point["lat"], point["lon"], days_ahead=16)
            
            initial_state = {
                "moisture": point["moisture"],
                "capacity": point["capacity"],
                "days_dry": point["days_dry"],
                "snow_cover": point["snow_cover"],
            }
            
            forecast_results = simulate_forecast(initial_state, forecast, soil_params)
            all_forecasts.append({
                "point": point,
                "forecast": forecast_results,
            })
            
            if verbose:
                print(f"   [{idx+1}/{len(forecast_points)}] км {point['distance_km']:.0f} ✓")
        
        except Exception as e:
            if verbose:
                print(f"   [{idx+1}/{len(forecast_points)}] км {point['distance_km']:.0f} ⚠️ {e}")
    
    if not all_forecasts:
        return None
    
    # Агрегируем: для каждого дня считаем средний % сухих точек
    capacity = all_forecasts[0]["point"]["capacity"]
    dry_threshold = capacity * 0.20
    
    # Собираем по датам
    dates = [f["forecast"][0]["date"] for f in all_forecasts if f["forecast"]]
    if not dates:
        return None
    
    num_days = len(all_forecasts[0]["forecast"])
    daily_stats = []
    
    for day_idx in range(num_days):
        date = all_forecasts[0]["forecast"][day_idx]["date"]
        
        dry_count = 0
        wet_count = 0
        mud_count = 0
        swamp_count = 0
        total_moisture = 0
        
        for f in all_forecasts:
            if day_idx < len(f["forecast"]):
                m = f["forecast"][day_idx]["moisture"]
                c = f["forecast"][day_idx]["capacity"]
                total_moisture += m

                _, status_key = get_status(m, c)
                if status_key == "dry":
                    dry_count += 1
                elif status_key == "wet":
                    wet_count += 1
                elif status_key == "mud":
                    mud_count += 1
                else:
                    swamp_count += 1
        
        n = len(all_forecasts)
        daily_stats.append({
            "date": date,
            "avg_moisture": total_moisture / n,
            "dry_pct": dry_count / n * 100,
            "wet_pct": wet_count / n * 100,
            "mud_pct": mud_count / n * 100,
            "swamp_pct": swamp_count / n * 100,
        })
    
    # Когда 80% станет "сухо" или "влажно" (можно катать)
    rideable_date = None
    dry_date = None
    
    for ds in daily_stats:
        if rideable_date is None and (ds["dry_pct"] + ds["wet_pct"]) >= 80:
            rideable_date = ds["date"]
        if dry_date is None and ds["dry_pct"] >= 80:
            dry_date = ds["date"]
    
    return {
        "num_points": len(all_forecasts),
        "daily_stats": daily_stats,
        "rideable_date": rideable_date,  # 80% сухо+влажно
        "dry_date": dry_date,             # 80% сухо
        "capacity": capacity,
    }


def get_trail_verdict(dry_pct, wet_pct, mud_pct, swamp_pct):
    """
    Вердикт по трейлу на основе распределения статусов.
    4 уровня:
    - МОЖНО: >= 70% сухо
    - СКОРЕЕ МОЖНО: >= 50% сухо ИЛИ (сухо+влажно >= 80% И сухо >= 30%)
    - СКОРЕЕ НЕЛЬЗЯ: сухо+влажно >= 50% но не дотягивает до "скорее можно"
    - НЕЛЬЗЯ: грязь+месиво > 50%
    """
    good = dry_pct
    ok = dry_pct + wet_pct

    if good >= 70:
        return "✅ МОЖНО", 4
    elif good >= 50 or (ok >= 80 and good >= 30):
        return "🟢 СКОРЕЕ МОЖНО", 3
    elif ok >= 50:
        return "🟠 СКОРЕЕ НЕЛЬЗЯ", 2
    else:
        return "🔴 НЕЛЬЗЯ", 1


def print_precipitation_log(results):
    """Печатаем таблицу осадков по дням (по первой точке с погодными данными)."""
    weather_result = next((r for r in results if "weather" in r), None)
    if not weather_result:
        return

    daily = weather_result["weather"]["daily"]
    dates = daily["time"]
    rain = daily["rain_sum"]
    snow = daily["snowfall_sum"]

    print(f"\n{'─'*60}")
    print(f"🌧  ОСАДКИ ЗА ПЕРИОД (точка km {weather_result['distance_km']:.0f})")
    print(f"{'Дата':<12} {'Дождь мм':>10} {'Снег см':>9}  {'':}")
    print(f"{'─'*40}")

    days_since_rain = 0
    last_rain_date = None

    for d, r, s in zip(dates, rain, snow):
        r = r or 0.0
        s = s or 0.0
        flag = ""
        if r >= RAIN_THRESHOLD or s * 10 >= RAIN_THRESHOLD:
            flag = "← дождь/снег"
            last_rain_date = d
            days_since_rain = 0
        else:
            days_since_rain += 1
        print(f"{d[5:]:<12} {r:>9.1f} {s:>9.1f}  {flag}")

    print(f"{'─'*40}")
    if last_rain_date:
        target = dates[-1]
        t0 = datetime.strptime(target, "%Y-%m-%d")
        tl = datetime.strptime(last_rain_date, "%Y-%m-%d")
        days_ago = (t0 - tl).days
        print(f"💧 Последние осадки: {last_rain_date} ({days_ago} дн. до {target})")
    else:
        print(f"💧 Осадков за период не зафиксировано")


def print_summary(results, total_distance, forecast_info, soil_params):
    """Печатаем красивый итог"""
    
    print(f"\n{'='*60}")
    print(f"📊 ИТОГО ПО ТРЕЙЛУ ({total_distance:.1f} км)")
    print(f"{'='*60}")
    
    agg = aggregate_status(results)
    
    # Красивые полоски
    bar_width = 40
    for key in ["dry", "wet", "mud", "swamp"]:
        if key in agg:
            info = agg[key]
            filled = int(info["percent"] / 100 * bar_width)
            bar = "█" * filled + "░" * (bar_width - filled)
            print(f"{info['label']:<12} {bar} {info['percent']:>5.1f}%")
    
    # Вердикт
    print(f"\n{'─'*60}")
    
    dry_pct = agg.get("dry", {}).get("percent", 0)
    wet_pct = agg.get("wet", {}).get("percent", 0)
    mud_pct = agg.get("mud", {}).get("percent", 0)
    swamp_pct = agg.get("swamp", {}).get("percent", 0)
    
    verdict, _ = get_trail_verdict(dry_pct, wet_pct, mud_pct, swamp_pct)
    print(f"🎯 {verdict}")
    
    # Прогноз
    if forecast_info:
        print(f"\n{'─'*60}")
        print(f"🔮 ПРОГНОЗ (на основе {forecast_info['num_points']} точек):")
        
        # Показываем таблицу прогноза с вердиктами
        print(f"\n{'Дата':<12} {'Сухо':>6} {'Влажно':>7} {'Грязь':>6} {'Месиво':>7}  {'Вердикт':<20}")
        print("-" * 70)
        
        for ds in forecast_info["daily_stats"][:12]:  # первые 12 дней
            date_short = ds["date"][5:]  # убираем год
            v, _ = get_trail_verdict(ds["dry_pct"], ds["wet_pct"], ds["mud_pct"], ds["swamp_pct"])
            print(f"{date_short:<12} {ds['dry_pct']:>5.0f}% {ds['wet_pct']:>6.0f}% "
                  f"{ds['mud_pct']:>5.0f}% {ds['swamp_pct']:>6.0f}%  {v:<20}")
        
        print()
        
        # Находим даты переходов между статусами
        prev_verdict = None
        transitions = []
        for ds in forecast_info["daily_stats"]:
            v, level = get_trail_verdict(ds["dry_pct"], ds["wet_pct"], ds["mud_pct"], ds["swamp_pct"])
            if v != prev_verdict:
                transitions.append((ds["date"], v, level))
                prev_verdict = v
        
        # Сортируем по уровню статуса и показываем первое вхождение каждого
        seen_levels = set()
        sorted_transitions = []
        for date_str, v, level in transitions:
            if level not in seen_levels:
                sorted_transitions.append((date_str, v, level))
                seen_levels.add(level)
        
        # Сортируем по уровню (1=нельзя, 2=скорее нельзя, 3=скорее можно, 4=можно)
        sorted_transitions.sort(key=lambda x: x[2])
        
        today = datetime.now().date()
        for date_str, v, level in sorted_transitions:
            dt = datetime.strptime(date_str, "%Y-%m-%d").date()
            days_until = (dt - today).days
            if days_until >= 0:
                print(f"{v}: {date_str} (через {days_until} дн)")
            else:
                print(f"{v}: сейчас")
    
    print(f"\n📖 Почва: {soil_params['name']}")
    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description='Trail Moisture v4 — GPX анализ')
    parser.add_argument('--gpx', type=str, required=True, help='Путь к GPX файлу')
    parser.add_argument('--soil', type=str, default=None,
                        help='Тип почвы: sand, sandy_loam, loam, silt_loam, clay_loam, clay, chernozem')
    parser.add_argument('--sample-km', type=float, default=5.0,
                        help='Интервал сэмплирования в км (по умолчанию 5)')
    parser.add_argument('--no-forecast', action='store_true', help='Не делать прогноз')
    parser.add_argument('--date', type=str, default=None,
                        help='Целевая дата в формате YYYY-MM-DD (по умолчанию сегодня). '
                             'При указании прошедшей даты прогноз отключается.')
    args = parser.parse_args()

    target_date = args.date
    display_date = target_date or datetime.now().strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"🚵 TRAIL MOISTURE INDEX v4 (GPX Analysis)")
    print(f"📅 Анализ на дату: {display_date}")
    print(f"{'='*60}\n")

    soil_params = get_soil_params(args.soil)
    print(f"🌍 Почва: {soil_params['name']}")
    print()

    # Анализ трейла
    results, total_distance = analyze_trail(
        args.gpx,
        soil_params,
        sample_km=args.sample_km,
        verbose=True,
        target_date=target_date,
    )

    # Осадки по дням
    print_precipitation_log(results)

    # Прогноз — только для текущей/будущей даты
    forecast_info = None
    is_historical = target_date and target_date < datetime.now().strftime("%Y-%m-%d")
    if not args.no_forecast and not is_historical:
        forecast_info = forecast_trail_drying(results, soil_params, verbose=True)

    # Итог
    print_summary(results, total_distance, forecast_info, soil_params)


if __name__ == "__main__":
    main()
