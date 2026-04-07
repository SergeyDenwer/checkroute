#!/usr/bin/env python3
"""
Trail Moisture Index Calculator v4
С поддержкой GPX треков — анализ множества точек вдоль маршрута.

Использование:
  python trail_moisture_v4.py --gpx trail.gpx
  python trail_moisture_v4.py --gpx trail.gpx --soil chernozem
  python trail_moisture_v4.py --gpx trail.gpx --sample-km 3
"""

import logging
import time
import requests
from datetime import datetime, timedelta
import argparse
import math

logger = logging.getLogger(__name__)
import gpxpy
from collections import defaultdict


SOIL_PARAMS = {"capacity": 15.0, "desorptivity": 3.5, "stage1_ratio": 0.5}

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


def clear_sky_radiation(lat_deg: float, day_of_year: int) -> float:
    """Ожидаемая ясная радиация МДж/м²/день (FAO Ra × 0.75).
    Используется как базовая линия для нормализации реальной радиации.
    Источник: FAO Irrigation and Drainage Paper 56, глава 3.
    """
    lat = math.radians(lat_deg)
    dr = 1 + 0.033 * math.cos(2 * math.pi * day_of_year / 365)
    d = 0.409 * math.sin(2 * math.pi * day_of_year / 365 - 1.39)
    arg = -math.tan(lat) * math.tan(d)
    if arg > 1:
        return 0.1   # полярная ночь — минимум
    ws = math.pi if arg < -1 else math.acos(arg)  # полночное солнце
    Ra = (24 * 60 / math.pi) * 0.0820 * dr * (
        ws * math.sin(lat) * math.sin(d) +
        math.cos(lat) * math.cos(d) * math.sin(ws)
    )
    return max(0.1, Ra * 0.75)  # ясное небо ≈ 75% от экзатмосферной


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


def adaptive_sample_km(total_km: float) -> float:
    """Шаг выборки в зависимости от длины маршрута."""

    if total_km <= 50:
        return 2.0
    elif total_km <= 100:
        return 3.0
    elif total_km <= 150:
        return 4.0
    else:
        return 5.0


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


def compute_point_slopes(raw_points, sampled, window_km=0.5):
    """Угол уклона (радианы) для каждой сэмплированной точки.
    Считает перепад высот в окне ±window_km по треку.
    Возвращает список float той же длины что sampled.
    """
    if not raw_points:
        return [0.0] * len(sampled)

    # Аннотируем raw_points накопленными расстояниями
    cum_dists = [0.0]
    for i in range(1, len(raw_points)):
        cum_dists.append(
            cum_dists[-1] + haversine_distance(
                raw_points[i-1][0], raw_points[i-1][1],
                raw_points[i][0], raw_points[i][1],
            )
        )

    slopes = []
    for _, _, _, dist_km in sampled:
        lo, hi = dist_km - window_km, dist_km + window_km
        window_pts = [
            (raw_points[i][2], cum_dists[i])
            for i in range(len(raw_points))
            if lo <= cum_dists[i] <= hi
            and raw_points[i][2] is not None
            and raw_points[i][2] > 0
        ]
        # Минимум 3 точки — одна пара может быть GPS-выбросом
        if len(window_pts) < 3:
            slopes.append(0.0)
            continue
        elevs = [p[0] for p in window_pts]
        horiz_m = (window_pts[-1][1] - window_pts[0][1]) * 1000.0
        # Используем IQR-устойчивый перепад: разница 90-го и 10-го перцентилей
        # вместо max-min, чтобы GPS-выбросы не раздували уклон
        elevs_sorted = sorted(elevs)
        n_e = len(elevs_sorted)
        elev_diff = elevs_sorted[int(n_e * 0.9)] - elevs_sorted[int(n_e * 0.1)]
        slope = math.atan(elev_diff / horiz_m) if horiz_m >= 1.0 else 0.0
        if slope > math.radians(10):  # логируем только заметные уклоны (>10°)
            logger.info(
                "slope km=%.1f: elev_diff=%.0fm horiz=%.0fm → %.1f° (cap_factor=%.2f)",
                dist_km, elev_diff, horiz_m, math.degrees(slope),
                max(0.65, 1.0 - math.tan(slope) * 100 * 0.007),
            )
        slopes.append(slope)
    return slopes


# Покрытия, которые не имеют смысла анализировать (грязи там нет)
PAVED_SURFACES = {"asphalt", "paved", "concrete", "sett"}


def get_point_at_distance(points, target_dist_km):
    """
    Возвращает (lat, lon, elev, cum_dist) — точку трека на расстоянии
    target_dist_km от начала. None если target_dist_km за пределами трека.
    """
    cum = 0.0
    for i in range(1, len(points)):
        d = haversine_distance(points[i-1][0], points[i-1][1], points[i][0], points[i][1])
        cum += d
        if cum >= target_dist_km:
            return points[i][0], points[i][1], points[i][2], cum
    return None



def fetch_weather_data(lat, lon, days_back=14):
    """Получаем данные погоды из Open-Meteo"""
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    
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
            "shortwave_radiation_sum",
            "wind_speed_10m_mean",
        ]),
        "wind_speed_unit": "ms",
        "timezone": "auto"
    }

    response = requests.get(url, params=params, timeout=15)
    if response.status_code != 200:
        raise Exception(f"API Error: {response.status_code}")
    return response.json()


def fetch_forecast(lat, lon, days_ahead=16):
    """Получаем прогноз погоды. Retry с backoff при 429."""
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
            "shortwave_radiation_sum",
            "wind_speed_10m_mean",
        ]),
        "wind_speed_unit": "ms",
        "forecast_days": days_ahead,
        "timezone": "auto"
    }

    for attempt in range(4):
        response = requests.get(url, params=params, timeout=15)
        if response.status_code == 200:
            return response.json()
        if response.status_code == 429:
            wait = 2 ** attempt  # 1, 2, 4, 8 секунд
            logger.warning("fetch_forecast 429 rate-limit lat=%.4f lon=%.4f, retry in %ds (attempt %d/4)",
                           lat, lon, wait, attempt + 1)
            time.sleep(wait)
            continue
        raise Exception(f"Forecast API Error: {response.status_code}")
    raise Exception("Forecast API Error: 429 (exhausted retries)")


def _simulate_day(temp_mean, rain, snowfall_cm, eto, surface_moisture, snow_cover, wet_index,
                  stage2_days, soil_params, radiation=0.0, expected_radiation=1.0, wind_speed=0.0):
    """Один шаг симуляции влажности (один день).

    stage2_days — реальный счётчик суток в Stage 2 с момента последнего промокания.
    wet_index   — экспоненциальная память о дождях (остаётся для rain_signal логики).
    radiation   — реальная радиация МДж/м²/день (shortwave_radiation_sum от Open-Meteo).
    expected_radiation — ясная радиация для этой широты/даты (FAO clear-sky Ra×0.75).
    wind_speed  — средняя скорость ветра м/с (wind_speed_10m_mean).
    """
    DESORPTIVITY = soil_params["desorptivity"]
    CAPACITY = soil_params["capacity"]
    STAGE1_THRESHOLD = CAPACITY * soil_params["stage1_ratio"]
    # snow_factor < 1 для быстродренирующих поверхностей: снег стекает, не блокируя испарение
    SNOW_FACTOR = soil_params.get("snow_factor", 1.0)
    # rain_factor < 1 для леса: часть осадков перехватывается кронами
    RAIN_FACTOR = soil_params.get("rain_factor", 1.0)

    # Open-Meteo snowfall_sum в cm снежного покрова (глубина, ~1мм воды = 1 см снега)
    # × 10 → мм снежного покрова; × 0.1 → мм SWE (плотность снега ~10%, стандарт)
    snowfall_mm = snowfall_cm * 10
    water_input = rain * RAIN_FACTOR

    snow_cover += snowfall_mm
    if temp_mean > 0 and snow_cover > 0:
        # Degree-day factor 3.0 мм/°С/день — нижний край для лесных трейлов
        # Источник: Rango & Martinec (1995); диапазон 1.5–3.5 для леса, 4–8 для открытых склонов
        melt_potential = temp_mean * 3.0
        snow_water = snow_cover * 0.1   # SWE = 10% от глубины снега
        actual_melt = min(snow_water, melt_potential)
        snow_cover -= actual_melt * 10
        snow_cover = max(0, snow_cover)
        # Для быстродренирующих поверхностей (gravel, rock) большая часть талой воды
        # стекает немедленно и не задерживается на поверхности
        water_input += actual_melt * SNOW_FACTOR

    # Плавное обновление wet_index: каждый мм дождя вносит вклад, каждый сухой день гасит 15%
    rain_signal = water_input / (water_input + 5.0) if water_input > 0 else 0.0
    wet_index = min(1.0, wet_index * 0.85 + rain_signal)

    if snow_cover * SNOW_FACTOR > 5:
        evaporation = 0.05
        stage2_days = 0  # под снегом счётчик сушки не идёт
    elif surface_moisture > STAGE1_THRESHOLD:
        # Stage 1: свободное испарение, лимитировано энергией.
        # Поправка через реальную радиацию: северный/затенённый склон получает меньше
        # солнца чем горизонтальная поверхность, для которой рассчитан ETO.
        # Применяем ТОЛЬКО когда данные реально присутствуют: radiation > 5% expected.
        # Нули в данных API → отсутствующие данные, не реальная темнота.
        if expected_radiation > 0 and radiation >= expected_radiation * 0.05:
            rad_factor = max(0.3, min(1.4, radiation / expected_radiation))
        else:
            rad_factor = 1.0
        # Ветер применяем только когда есть данные (wind_speed > 0).
        # Тихая погода (0 м/с) физически возможна, но 0 как "нет данных" — нет.
        if wind_speed > 0:
            wind_factor = max(0.75, min(1.25, 1.0 + (wind_speed - 3.0) * 0.04))
        else:
            wind_factor = 1.0
        evaporation = eto * 0.9 * rad_factor * wind_factor
        if rad_factor != 1.0 or wind_factor != 1.0:
            logger.debug(
                "  Stage1 ETO=%.2f rad_f=%.2f(rad=%.1f/exp=%.1f) wind_f=%.2f(%.1fm/s) → evap=%.2f",
                eto, rad_factor, radiation, expected_radiation, wind_factor, wind_speed, evaporation,
            )
        stage2_days = 0  # в Stage 1 счётчик сбрасывается
    else:
        # Stage 2: Philip's sorptivity formula с реальным счётчиком суток.
        # dE(t) = S × (√t − √(t−1)), где S = DESORPTIVITY [мм/√день].
        # Источник: Philip (1957), Leeds-Harrison et al. (1994).
        stage2_days += 1
        t = stage2_days
        str_factor = math.sqrt(t) - math.sqrt(t - 1)
        evaporation = DESORPTIVITY * str_factor

    evaporation = min(evaporation, surface_moisture)
    surface_moisture = surface_moisture + water_input - evaporation
    surface_moisture = max(0, min(surface_moisture, CAPACITY))

    # Если вода вернула нас в Stage 1 — счётчик Stage 2 сбрасываем на старте следующего дня
    if surface_moisture > STAGE1_THRESHOLD:
        stage2_days = 0

    return surface_moisture, snow_cover, wet_index, stage2_days


def simulate_moisture(weather_data, soil_params):
    """Симуляция влажности поверхностного слоя по историческим данным."""
    daily = weather_data["daily"]
    surface_moisture = 0.0
    snow_cover = 0.0
    wet_index = 0.0
    stage2_days = 0

    lat = weather_data.get("latitude", 45.0)
    rads  = [r or 0.0 for r in daily.get("shortwave_radiation_sum", [])]
    winds = [w or 0.0 for w in daily.get("wind_speed_10m_mean", [])]

    # Логируем качество входных данных — ключ к диагностике аномалий
    n_days = len(daily.get("time", []))
    rad_present  = sum(1 for r in rads if r > 0)
    wind_present = sum(1 for w in winds if w > 0)
    mean_rad  = (sum(rads)  / len(rads))  if rads  else 0.0
    mean_wind = (sum(winds) / len(winds)) if winds else 0.0
    total_rain = sum(daily.get("rain_sum") or [0])
    logger.info(
        "simulate_moisture lat=%.4f days=%d rain_total=%.1fmm "
        "radiation=%d/%d days (mean=%.1f MJ) wind=%d/%d days (mean=%.1f m/s) "
        "capacity=%.2f desorpt=%.2f rain_f=%.2f",
        lat, n_days, total_rain,
        rad_present, n_days, mean_rad,
        wind_present, n_days, mean_wind,
        soil_params["capacity"], soil_params["desorptivity"],
        soil_params.get("rain_factor", 1.0),
    )

    for i in range(len(daily["time"])):
        try:
            doy = datetime.strptime(daily["time"][i], "%Y-%m-%d").timetuple().tm_yday
            expected_rad = clear_sky_radiation(lat, doy)
        except Exception:
            expected_rad = 1.0

        surface_moisture, snow_cover, wet_index, stage2_days = _simulate_day(
            temp_mean=daily["temperature_2m_mean"][i] or 0,
            rain=daily["rain_sum"][i] or 0,
            snowfall_cm=daily["snowfall_sum"][i] or 0,
            eto=daily["et0_fao_evapotranspiration"][i] or 0,
            surface_moisture=surface_moisture,
            snow_cover=snow_cover,
            wet_index=wet_index,
            stage2_days=stage2_days,
            soil_params=soil_params,
            radiation=rads[i] if i < len(rads) else 0.0,
            expected_radiation=expected_rad,
            wind_speed=winds[i] if i < len(winds) else 0.0,
        )

    return {
        "moisture": surface_moisture,
        "capacity": soil_params["capacity"],
        "wet_index": wet_index,
        "snow_cover": snow_cover,
        "stage2_days": stage2_days,
    }


def simulate_forecast(initial_state, forecast_data, soil_params):
    """Симуляция с прогнозом погоды, возвращает результаты по дням."""
    daily = forecast_data["daily"]
    surface_moisture = initial_state["moisture"]
    snow_cover = initial_state.get("snow_cover", 0)
    wet_index = initial_state.get("wet_index", 0.0)
    stage2_days = initial_state.get("stage2_days", 0)

    lat = forecast_data.get("latitude", 45.0)
    rads  = [r or 0.0 for r in daily.get("shortwave_radiation_sum", [])]
    winds = [w or 0.0 for w in daily.get("wind_speed_10m_mean", [])]
    n_days = len(daily.get("time", []))
    logger.info(
        "simulate_forecast lat=%.4f days=%d moisture_init=%.2f cap=%.2f desorpt=%.2f",
        lat, n_days, surface_moisture, soil_params["capacity"], soil_params["desorptivity"],
    )
    results = []

    for i in range(len(daily["time"])):
        temp_mean = ((daily["temperature_2m_max"][i] or 0) + (daily["temperature_2m_min"][i] or 0)) / 2
        try:
            doy = datetime.strptime(daily["time"][i], "%Y-%m-%d").timetuple().tm_yday
            expected_rad = clear_sky_radiation(lat, doy)
        except Exception:
            expected_rad = 1.0

        surface_moisture, snow_cover, wet_index, stage2_days = _simulate_day(
            temp_mean=temp_mean,
            rain=daily["rain_sum"][i] or 0,
            snowfall_cm=daily["snowfall_sum"][i] or 0,
            eto=daily["et0_fao_evapotranspiration"][i] or 0,
            surface_moisture=surface_moisture,
            snow_cover=snow_cover,
            wet_index=wet_index,
            stage2_days=stage2_days,
            soil_params=soil_params,
            radiation=rads[i] if i < len(rads) else 0.0,
            expected_radiation=expected_rad,
            wind_speed=winds[i] if i < len(winds) else 0.0,
        )
        results.append({
            "date": daily["time"][i],
            "moisture": surface_moisture,
            "capacity": soil_params["capacity"],
            "rain": daily["rain_sum"][i] or 0,
            "wet_index": wet_index,
            "snow_cover": snow_cover,
            "stage2_days": stage2_days,
        })

    return results


# Как OSM surface влияет на физику симуляции:
# capacity_mult     — удержание воды относительно суглинка (dirt=1.0, ~35–45% VWC)
#                     gravel ~3–5% VWC → 0.15; sand ~10–25% VWC → 0.40
#                     Источник: Cornell NRCCA, Oklahoma State Extension, IAEA TCS-30
# desorptivity_mult — скорость высыхания Stage 2 (сорптивность Philip)
#                     sand S≈4 мм·с⁻¹/², loam S≈1, clay S≈0.1; gravel free-draining
#                     Источник: Leeds-Harrison et al. (1994), Youngs (1968)
# snow_factor       — доля snow_cover, реально блокирующая испарение
#                     = 1 − runoff_coeff (Rational Method / USDA TR-55)
#                     gravel C≈0.7 → snow_factor≈0.25; bare soil C≈0.35 → snow_factor≈0.75
#                     Источник: USDA TR-55 Table 2-2a, California Water Boards
SURFACE_SOIL_MODIFIERS = {
    "asphalt":      {"capacity_mult": 0.15, "desorptivity_mult": 4.0,  "snow_factor": 0.1},
    "paved":        {"capacity_mult": 0.15, "desorptivity_mult": 4.0,  "snow_factor": 0.1},
    "concrete":     {"capacity_mult": 0.15, "desorptivity_mult": 4.0,  "snow_factor": 0.1},
    "gravel":       {"capacity_mult": 0.15, "desorptivity_mult": 4.5,  "snow_factor": 0.25},
    "fine_gravel":  {"capacity_mult": 0.20, "desorptivity_mult": 3.5,  "snow_factor": 0.25},
    "compacted":    {"capacity_mult": 0.55, "desorptivity_mult": 1.4,  "snow_factor": 0.45},
    "dirt":         {"capacity_mult": 1.0,  "desorptivity_mult": 1.0,  "snow_factor": 0.75},
    "ground":       {"capacity_mult": 1.0,  "desorptivity_mult": 1.0,  "snow_factor": 0.75},
    "unpaved":      {"capacity_mult": 1.0,  "desorptivity_mult": 1.0,  "snow_factor": 0.75},
    "grass":        {"capacity_mult": 1.10, "desorptivity_mult": 0.85, "snow_factor": 1.0},
    "sand":         {"capacity_mult": 0.40, "desorptivity_mult": 3.0,  "snow_factor": 0.65},
    "mud":          {"capacity_mult": 1.30, "desorptivity_mult": 0.65, "snow_factor": 1.0},
}


OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_STATUS_URL = "https://overpass-api.de/api/status"

# OSM highway types that are implicitly paved even without a surface tag
_PAVED_HIGHWAY_TYPES = {
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link",
    "residential", "living_street", "service", "road",
}


def _overpass_wait_for_slot(timeout: int = 60) -> bool:
    """
    Проверяет статус Overpass API и ждёт освобождения слота.
    Возвращает True если слот доступен, False если timeout истёк.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(OVERPASS_STATUS_URL, timeout=5)
            if resp.status_code != 200:
                time.sleep(2)
                continue
            # Парсим строку вида "2 slots available now." или "Slot available after: 2026-03-10T..."
            text = resp.text
            if "available now" in text:
                return True
            # Ищем время следующего слота
            import re
            m = re.search(r"Slot available after: (\S+)", text)
            if m:
                try:
                    slot_time = datetime.fromisoformat(m.group(1).rstrip("Z"))
                    wait = max(0, (slot_time - datetime.utcnow()).total_seconds()) + 1
                    logger.info("Overpass: slot available in %.0fs, waiting...", wait)
                    time.sleep(min(wait, deadline - time.time()))
                    continue
                except ValueError:
                    pass
            # Если не распарсили — ждём немного и пробуем снова
            time.sleep(3)
        except Exception as e:
            logger.warning("Overpass status check failed: %s", e)
            time.sleep(3)
    return False


def _fetch_terrain_bbox(south: float, west: float, north: float, east: float):
    """Один Overpass bbox-запрос — все highway ways + лесные полигоны в прямоугольнике.

    Возвращает (highway_ways, forest_polys) или (None, None) при ошибке.
    Каждый элемент: {"tags": {...}, "geometry": [{"lat":, "lon":}, ...]}

    Примечание: мультиполигонные relation обрабатываются упрощённо —
    геометрии всех членов конкатенируются. Для простых лесных полигонов
    (closed way) работает точно.
    """
    buf = 0.002  # ~220м буфер вокруг bbox
    # Relation лесных полигонов намеренно исключены: при конкатенации
    # геометрий членов relation получается некорректный полигон, что даёт
    # ложные срабатывания point-in-polygon на больших territory/заповедников.
    # Только closed ways — простые, надёжно определяемые полигоны.
    query = (
        f"[out:json][timeout:90];"
        f"("
        f"  way({south-buf:.6f},{west-buf:.6f},{north+buf:.6f},{east+buf:.6f})[highway];"
        f"  way({south-buf:.6f},{west-buf:.6f},{north+buf:.6f},{east+buf:.6f})[natural=wood];"
        f"  way({south-buf:.6f},{west-buf:.6f},{north+buf:.6f},{east+buf:.6f})[landuse=forest];"
        f");"
        f"out tags geom;"
    )

    if not _overpass_wait_for_slot(timeout=90):
        logger.warning("terrain_bbox: no Overpass slot for bbox (%.4f,%.4f,%.4f,%.4f)",
                       south, west, north, east)
        return None, None

    for attempt in range(3):
        try:
            resp = requests.post(OVERPASS_URL, data={"data": query}, timeout=100)
        except Exception as e:
            logger.warning("terrain_bbox exception: %s", e)
            return None, None

        if resp.status_code == 429:
            if not _overpass_wait_for_slot(timeout=90):
                return None, None
            continue
        if resp.status_code in (503, 504):
            time.sleep(5 * (attempt + 1))
            continue
        if resp.status_code != 200:
            logger.warning("terrain_bbox HTTP %s", resp.status_code)
            return None, None

        elements = resp.json().get("elements", [])
        highway_ways = []
        forest_polys = []

        for el in elements:
            tags = el.get("tags", {})
            geom = el.get("geometry") or []

            if not geom:
                continue

            if "highway" in tags:
                highway_ways.append({"tags": tags, "geometry": geom})
            # Только closed ways (первая == последняя точка) — надёжные полигоны
            if (tags.get("natural") == "wood" or tags.get("landuse") == "forest"):
                if len(geom) >= 4 and geom[0] == geom[-1]:
                    forest_polys.append({"tags": tags, "geometry": geom})

        logger.info("terrain_bbox (%.4f,%.4f,%.4f,%.4f): %d highway, %d forest polygons",
                    south, west, north, east, len(highway_ways), len(forest_polys))
        return highway_ways, forest_polys

    return None, None


def _way_dist_sq(lat: float, lon: float, geom: list) -> float:
    """Минимальное расстояние² от точки до ломаной линии.

    Единицы — градусы² со скоррекцией cos(lat) по оси X.
    50 м ≈ 0.00045° → порог 50 м² ≈ 2×10⁻⁷ °².
    """
    cos_lat = math.cos(math.radians(lat))
    px = lon * cos_lat
    py = lat
    min_d = float("inf")

    for i in range(len(geom) - 1):
        ax = geom[i]["lon"] * cos_lat
        ay = geom[i]["lat"]
        bx = geom[i + 1]["lon"] * cos_lat
        by = geom[i + 1]["lat"]
        dx, dy = bx - ax, by - ay
        len_sq = dx * dx + dy * dy
        if len_sq < 1e-16:
            d = (px - ax) ** 2 + (py - ay) ** 2
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len_sq))
            d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
        if d < min_d:
            min_d = d

    return min_d


def _point_in_polygon(lat: float, lon: float, geom: list) -> bool:
    """Ray casting алгоритм — точка внутри полигона?

    geom: список {"lat":, "lon":} (первый == последний для closed way).
    """
    n = len(geom)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = geom[i]["lon"], geom[i]["lat"]
        xj, yj = geom[j]["lon"], geom[j]["lat"]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def fetch_terrain_info_bulk(sampled_points: list, chunk_size: int = 30) -> list:
    """Получает surface + forest для всех точек за минимум Overpass-запросов.

    Разбивает точки на чанки по chunk_size, для каждого чанка делает
    один bbox-запрос. Типичный маршрут 30–50 км → 1 запрос вместо 15–25.
    При ошибке bbox-запроса чанк деградирует к поштучным вызовам fetch_terrain_info.

    sampled_points: list of (lat, lon, elev, dist_km)
    Возвращает list[dict] той же длины: {"surface", "is_forest", "leaf_type"}
    """
    # Порог близости к дороге: 50 м ≈ 0.00045° → 50м² ≈ 2×10⁻⁷ °²
    PROXIMITY_SQ = (50.0 / 111_000.0) ** 2

    n = len(sampled_points)
    results = [None] * n

    for chunk_start in range(0, n, chunk_size):
        indices = list(range(chunk_start, min(chunk_start + chunk_size, n)))
        pts = [sampled_points[i] for i in indices]

        lats = [p[0] for p in pts]
        lons = [p[1] for p in pts]
        south, north = min(lats), max(lats)
        west,  east  = min(lons), max(lons)

        highway_ways, forest_polys = _fetch_terrain_bbox(south, west, north, east)

        for local_i, pt_idx in enumerate(indices):
            lat, lon = pts[local_i][0], pts[local_i][1]

            if highway_ways is None:
                # Fallback: поштучный запрос для этой точки
                logger.info("terrain_bulk: bbox failed, falling back for point %d", pt_idx)
                results[pt_idx] = fetch_terrain_info(lat, lon)
                continue

            # --- Ближайший highway way ---
            best_way   = None
            best_dist  = PROXIMITY_SQ
            for way in highway_ways:
                geom = way["geometry"]
                if len(geom) < 2:
                    continue
                d = _way_dist_sq(lat, lon, geom)
                if d < best_dist:
                    best_dist = d
                    best_way  = way

            surface = "ground"
            if best_way:
                tags = best_way["tags"]
                if "surface" in tags:
                    surface = tags["surface"]
                elif tags.get("highway") in _PAVED_HIGHWAY_TYPES:
                    surface = "asphalt"
                # иначе оставляем "ground" — непомеченная тропа

            # --- Лес (point-in-polygon) ---
            is_forest = False
            leaf_type = ""
            for poly in forest_polys:
                if _point_in_polygon(lat, lon, poly["geometry"]):
                    is_forest = True
                    leaf_type = poly["tags"].get("leaf_type", "")
                    break

            logger.info(
                "terrain_bulk pt=%d (%.5f,%.5f) surface=%s forest=%s leaf=%s",
                pt_idx, lat, lon, surface,
                "YES" if is_forest else "no", leaf_type or "-",
            )

            results[pt_idx] = {
                "surface":   surface,
                "is_forest": is_forest,
                "leaf_type": leaf_type,
            }

    return results


def fetch_terrain_info(lat: float, lon: float) -> dict:
    """Тип покрытия и наличие лесного полога для точки — один Overpass-запрос.

    Возвращает:
        {"surface": str, "is_forest": bool, "leaf_type": str}

    surface:   OSM surface tag / 'asphalt' для дорожных highway / 'ground' по умолчанию / 'error'
    is_forest: True если точка находится внутри natural=wood или landuse=forest полигона
    leaf_type: 'needleleaved' | 'broadleaved' | 'mixed' | '' (из OSM leaf_type тега)
    """
    query = (
        f"[out:json][timeout:15];"
        f"("
        f"  way(around:30,{lat},{lon})[highway];"
        f")->.ways;"
        f"is_in({lat},{lon})->.a;"
        f"("
        f"  area.a[natural=wood];"
        f"  area.a[landuse=forest];"
        f")->.forests;"
        f"(.ways;.forests;);"
        f"out tags;"
    )

    result = {"surface": "ground", "is_forest": False, "leaf_type": ""}

    if not _overpass_wait_for_slot():
        logger.warning("terrain_info: Overpass no slot at (%.5f, %.5f) → error", lat, lon)
        result["surface"] = "error"
        return result

    for attempt in range(3):
        try:
            resp = requests.post(OVERPASS_URL, data={"data": query}, timeout=15)
        except Exception as e:
            logger.warning("terrain_info exception at (%.5f, %.5f): %s", lat, lon, e)
            result["surface"] = "error"
            return result

        if resp.status_code == 429:
            logger.warning("terrain_info: unexpected 429 at (%.5f, %.5f), waiting for slot...", lat, lon)
            if not _overpass_wait_for_slot():
                result["surface"] = "error"
                return result
            continue

        if resp.status_code in (503, 504):
            wait = 5 * (attempt + 1)
            logger.warning("terrain_info HTTP %s at (%.5f, %.5f), retry in %ds (attempt %d/3)",
                           resp.status_code, lat, lon, wait, attempt + 1)
            time.sleep(wait)
            continue

        if resp.status_code != 200:
            logger.warning("terrain_info HTTP %s at (%.5f, %.5f) → error", resp.status_code, lat, lon)
            result["surface"] = "error"
            return result

        elements = resp.json().get("elements", [])

        # Проверяем лес — area-элементы с нужными тегами
        for el in elements:
            tags = el.get("tags", {})
            if tags.get("natural") == "wood" or tags.get("landuse") == "forest":
                result["is_forest"] = True
                result["leaf_type"] = tags.get("leaf_type", "")
                logger.info("terrain_info (%.5f, %.5f) forest=True leaf_type=%s",
                            lat, lon, result["leaf_type"])

        # Определяем surface из highway ways
        highway_els = [el for el in elements if "highway" in el.get("tags", {})]
        if not highway_els:
            logger.debug("terrain_info (%.5f, %.5f) no highway ways → ground", lat, lon)
            return result

        for el in highway_els:
            if "surface" in el.get("tags", {}):
                result["surface"] = el["tags"]["surface"]
                logger.info("terrain_info (%.5f, %.5f) surface=%s forest=%s",
                            lat, lon, result["surface"], result["is_forest"])
                return result

        for el in highway_els:
            hw = el.get("tags", {}).get("highway")
            if hw in _PAVED_HIGHWAY_TYPES:
                result["surface"] = "asphalt"
                logger.info("terrain_info (%.5f, %.5f) no surface tag, highway=%s → asphalt", lat, lon, hw)
                return result

        hw_types = [el.get("tags", {}).get("highway") for el in highway_els]
        logger.info("terrain_info (%.5f, %.5f) highway=%s, no surface tag → ground", lat, lon, hw_types)
        return result

    logger.warning("terrain_info all retries failed at (%.5f, %.5f) → error", lat, lon)
    result["surface"] = "error"
    return result


def fetch_surface_type(lat: float, lon: float) -> str:
    """Обратная совместимость — тонкая обёртка над fetch_terrain_info."""
    return fetch_terrain_info(lat, lon)["surface"]


def apply_surface_modifiers(soil_params: dict, surface: str) -> dict:
    """
    Возвращает копию soil_params, скорректированную под тип покрытия.
    SOIL (геология) остаётся базой, surface (конструкция тропы) уточняет физику.
    """
    mods = SURFACE_SOIL_MODIFIERS.get(surface, {"capacity_mult": 1.0, "desorptivity_mult": 1.0, "snow_factor": 1.0})
    return {
        **soil_params,
        "capacity":        soil_params["capacity"]     * mods["capacity_mult"],
        "desorptivity":    soil_params["desorptivity"] * mods["desorptivity_mult"],
        "snow_factor":     mods.get("snow_factor",     1.0),
    }


def apply_forest_modifiers(soil_params: dict, is_forest: bool, leaf_type: str = "") -> dict:
    """Корректирует параметры под лесной полог.

    Лес замедляет испарение (меньше солнца и ветра под кронами)
    и перехватывает часть осадков (они испаряются с листьев, не достигая грунта).

    Коэффициенты:
      desorptivity × 0.50  — испарение из Stage 2 в 2× медленнее под пологом
                             (экранирование ветра и радиации)
      rain_factor  = 0.70  — хвойный лес (Horton 1919; перехват ~25–35%)
      rain_factor  = 0.80  — лиственный лес (Zinke 1967; перехват ~15–25%)
    """
    if not is_forest:
        return soil_params
    rain_factor = 0.70 if leaf_type == "needleleaved" else 0.80
    return {
        **soil_params,
        "desorptivity": soil_params["desorptivity"] * 0.50,
        "rain_factor":  rain_factor,
    }


def apply_slope_modifier(soil_params: dict, slope_rad: float) -> dict:
    """Корректирует параметры под уклон рельефа.

    Крутые склоны — вода стекает быстрее, меньше задерживается в верхнем слое.
    capacity уменьшается с ростом уклона.

    0°  → factor = 1.00  (без изменений)
    15° → factor ≈ 0.90  (уклон ~27%)
    30° → factor ≈ 0.77  (уклон ~58%)
    45° → factor = 0.65  (ограничение снизу)

    Источник: Horton (1945) overland flow; USDA TR-55 runoff curve numbers.
    Коэффициент намеренно консервативный: GPS elevation данные имеют шум.
    """
    slope_pct = math.tan(slope_rad) * 100.0   # процент уклона
    capacity_factor = max(0.65, 1.0 - slope_pct * 0.007)
    return {
        **soil_params,
        "capacity": soil_params["capacity"] * capacity_factor,
    }


def get_status(moisture, capacity):
    """Возвращает статус и ключ"""
    pct = moisture / capacity
    for threshold, label, key in STATUS_THRESHOLDS:
        if pct < threshold:
            return label, key
    return STATUS_THRESHOLDS[-1][1], STATUS_THRESHOLDS[-1][2]


def analyze_trail(gpx_file, sample_km=5.0, verbose=True):
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
            surface = fetch_surface_type(lat, lon)
            soil_params = apply_surface_modifiers(SOIL_PARAMS, surface)
            weather = fetch_weather_data(lat, lon, days_back=14)
            state = simulate_moisture(weather, soil_params)
            status_label, status_key = get_status(state["moisture"], state["capacity"])

            results.append({
                "lat": lat,
                "lon": lon,
                "elevation": elev,
                "distance_km": dist_km,
                "surface": surface,
                "moisture": state["moisture"],
                "capacity": state["capacity"],
                "wet_index": state["wet_index"],
                "snow_cover": state["snow_cover"],
                "status_label": status_label,
                "status_key": status_key,
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


def forecast_trail_drying(results, verbose=True):
    """
    Прогноз когда трейл станет сухим.
    Используем все non-paved точки — те же что для текущего статуса.
    """
    valid = [r for r in results if "moisture" in r]
    if not valid:
        return None

    # Ограничиваем до 3 точек (начало, середина, конец) — чтобы не долбить API
    MAX_FORECAST_POINTS = 3
    if len(valid) > MAX_FORECAST_POINTS:
        step = len(valid) / MAX_FORECAST_POINTS
        forecast_points = [valid[int(i * step)] for i in range(MAX_FORECAST_POINTS)]
    else:
        forecast_points = valid

    if verbose:
        print(f"\n🔮 Прогноз высыхания по {len(forecast_points)} точкам (из {len(valid)})...")
    
    all_forecasts = []
    _forecast_cache = {}  # (lat2, lon2) → forecast json

    for idx, point in enumerate(forecast_points):
        try:
            cache_key = (round(point["lat"], 2), round(point["lon"], 2))
            if cache_key in _forecast_cache:
                forecast = _forecast_cache[cache_key]
                logger.debug("forecast_trail_drying: cache hit for key=%s (km=%.1f)",
                             cache_key, point.get("distance_km", 0))
            else:
                forecast = fetch_forecast(point["lat"], point["lon"], days_ahead=16)
                _forecast_cache[cache_key] = forecast
            
            initial_state = {
                "moisture":    point["moisture"],
                "capacity":    point["capacity"],
                "wet_index":   point["wet_index"],
                "snow_cover":  point["snow_cover"],
                "stage2_days": point.get("stage2_days", 0),
            }
            soil_params = apply_surface_modifiers(SOIL_PARAMS, point.get("surface", "ground"))
            soil_params = apply_forest_modifiers(
                soil_params, point.get("is_forest", False), point.get("leaf_type", "")
            )
            soil_params = apply_slope_modifier(soil_params, point.get("slope_rad", 0.0))
            forecast_results = simulate_forecast(initial_state, forecast, soil_params)
            all_forecasts.append({
                "point": point,
                "forecast": forecast_results,
            })
            
            if verbose:
                print(f"   [{idx+1}/{len(forecast_points)}] км {point['distance_km']:.0f} ✓")
        
        except Exception as e:
            logger.warning(
                "forecast_trail_drying: point km=%.1f fetch failed: %s",
                point.get("distance_km", 0), e,
            )
            if verbose:
                print(f"   [{idx+1}/{len(forecast_points)}] км {point['distance_km']:.0f} ⚠️ {e}")
    
    if not all_forecasts:
        return None
    
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
        total_rain = 0

        for f in all_forecasts:
            if day_idx < len(f["forecast"]):
                m = f["forecast"][day_idx]["moisture"]
                c = f["forecast"][day_idx]["capacity"]
                total_moisture += m
                total_rain += f["forecast"][day_idx].get("rain", 0)

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
            "avg_rain": total_rain / n,
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


def print_summary(results, total_distance, forecast_info):
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
    
    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description='Trail Moisture v4 — GPX анализ')
    parser.add_argument('--gpx', type=str, required=True, help='Путь к GPX файлу')
    parser.add_argument('--sample-km', type=float, default=5.0,
                        help='Интервал сэмплирования в км (по умолчанию 5)')
    parser.add_argument('--no-forecast', action='store_true', help='Не делать прогноз')
    args = parser.parse_args()
    
    print(f"\n{'='*60}")
    print(f"🚵 TRAIL MOISTURE INDEX v4 (GPX Analysis)")
    print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")
    
    # Анализ трейла
    results, total_distance = analyze_trail(
        args.gpx,
        sample_km=args.sample_km,
        verbose=True
    )

    # Прогноз
    forecast_info = None
    if not args.no_forecast:
        forecast_info = forecast_trail_drying(results, verbose=True)

    # Итог
    print_summary(results, total_distance, forecast_info)


if __name__ == "__main__":
    main()
