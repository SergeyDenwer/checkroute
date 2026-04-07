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



def _http_get_retry(url, params=None, timeout=60, max_retries=10, base_delay=2):
    """
    GET-запрос с экспоненциальным backoff. Долбит до победного.
    Ретраит на 429 / 503 / 504 / timeout / сетевые ошибки.
    На не-ретраишных кодах (4xx кроме 429) сразу бросает исключение.
    max_retries=10: суммарное ожидание до ~6 минут при цепочке неудач.
    """
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 503, 504):
                delay = min(base_delay * (2 ** attempt), 120)
                logger.warning("GET %s HTTP %s, retry %d/%d через %ds",
                               url.split("?")[0], resp.status_code,
                               attempt + 1, max_retries, delay)
                time.sleep(delay)
                continue
            raise Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except requests.exceptions.Timeout:
            delay = min(base_delay * (2 ** attempt), 120)
            logger.warning("GET %s timeout, retry %d/%d через %ds",
                           url.split("?")[0], attempt + 1, max_retries, delay)
            time.sleep(delay)
        except Exception as e:
            if attempt < max_retries - 1:
                delay = min(base_delay * (2 ** attempt), 120)
                logger.warning("GET %s: %s, retry %d/%d через %ds",
                               url.split("?")[0], e, attempt + 1, max_retries, delay)
                time.sleep(delay)
            else:
                raise
    raise Exception(f"GET {url.split('?')[0]} не ответил за {max_retries} попыток")


def _http_post_retry(url, json=None, data=None, timeout=60, max_retries=8, base_delay=2):
    """POST-запрос с экспоненциальным backoff."""
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json=json, data=data, timeout=timeout)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 503, 504):
                delay = min(base_delay * (2 ** attempt), 120)
                logger.warning("POST %s HTTP %s, retry %d/%d через %ds",
                               url, resp.status_code, attempt + 1, max_retries, delay)
                time.sleep(delay)
                continue
            raise Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except requests.exceptions.Timeout:
            delay = min(base_delay * (2 ** attempt), 120)
            logger.warning("POST %s timeout, retry %d/%d через %ds",
                           url, attempt + 1, max_retries, delay)
            time.sleep(delay)
        except Exception as e:
            if attempt < max_retries - 1:
                delay = min(base_delay * (2 ** attempt), 120)
                logger.warning("POST %s: %s, retry %d/%d через %ds",
                               url, e, attempt + 1, max_retries, delay)
                time.sleep(delay)
            else:
                raise
    raise Exception(f"POST {url} не ответил за {max_retries} попыток")


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
        ]),
        "timezone": "auto"
    }
    
    return _http_get_retry(url, params=params, timeout=30).json()


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
            "wind_speed_10m_mean",
        ]),
        "forecast_days": days_ahead,
        "timezone": "auto"
    }

    return _http_get_retry(url, params=params, timeout=30).json()


def _simulate_day(temp_mean, rain, snowfall_cm, eto, surface_moisture, snow_cover, wet_index,
                  stage2_days, soil_params, *, wind_speed=None):
    """Один шаг симуляции влажности (один день).

    stage2_days — счётчик последовательных суток в Stage 2 (Philip's formula).
    Сбрасывается в 0 при переходе обратно в Stage 1 или под снегом.
    Источник: Philip (1957), Stroosnijder (1987).
    DESORPTIVITY [мм/√день] верифицируем: суглинок 2–4, песок 5–8, глина 0.5–1.5.

    soil_params ключи (помимо базовых capacity/desorptivity/stage1_ratio):
      eto_factor        — лесной полог × аспект-коэффициент испарения (по умолчанию 1.0)
      rain_factor       — перехват осадков кронами (по умолчанию 1.0)
      snow_factor       — доля талой воды, достигающей поверхности (по умолчанию 1.0)
      degree_day_factor — степень-день таяния снега мм/°С/день (по умолчанию 3.0);
                          модифицируется аспектом: юг→4.0, север→1.5 (Rango & Martinec 1995)
      is_forest         — bool, для Stage 2 wind bonus (под пологом ветер не сушит)

    wind_speed — скорость ветра м/с (wind_speed_10m_mean из Open-Meteo).
    Используется ТОЛЬКО в Stage 2 на открытых (не лесных) точках: конвекция у
    поверхности ускоряет испарение в первые дни Stage 2 (пока поверхность ещё влажная).
    Stage 1 уже содержит ветер через ETO (Penman-Monteith), двойной счёт исключён.
    Максимальный бонус 0.2 мм/день при ветре ≥8 м/с, только первые 5 суток Stage 2.

    wet_index ∈ [0, 1] — экспоненциальная память о последних дождях.
    После фикса Stage 2 (переход на stage2_days) wet_index НЕ участвует в расчёте
    испарения. Вычисляется и сохраняется как диагностическая метрика (UI, логи).
    """
    DESORPTIVITY      = soil_params["desorptivity"]
    CAPACITY          = soil_params["capacity"]
    STAGE1_THRESHOLD  = CAPACITY * soil_params["stage1_ratio"]
    SNOW_FACTOR       = soil_params.get("snow_factor",       1.0)
    ETO_FACTOR        = soil_params.get("eto_factor",        1.0)
    RAIN_FACTOR       = soil_params.get("rain_factor",       1.0)
    DEGREE_DAY_FACTOR = soil_params.get("degree_day_factor", 3.0)
    IS_FOREST         = soil_params.get("is_forest",         False)

    # Перехват осадков пологом (лес: −20%)
    rain = rain * RAIN_FACTOR

    snowfall_mm = snowfall_cm * 10
    water_input = rain

    snow_cover += snowfall_mm
    if temp_mean > 0 and snow_cover > 0:
        # Degree-day таяние: фактор модифицируется аспектом склона
        # юг: 4.0 мм/°С/день, север: 1.5, восток/запад/равнина: 3.0
        # Источник: Rango & Martinec (1995)
        melt_potential = temp_mean * DEGREE_DAY_FACTOR
        snow_water     = snow_cover * 0.1   # SWE = 10% глубины снега
        actual_melt    = min(snow_water, melt_potential)
        snow_cover    -= actual_melt * 10
        snow_cover     = max(0, snow_cover)
        water_input   += actual_melt * SNOW_FACTOR

    # Плавное обновление wet_index
    rain_signal = water_input / (water_input + 5.0) if water_input > 0 else 0.0
    wet_index = min(1.0, wet_index * 0.85 + rain_signal)

    if snow_cover * SNOW_FACTOR > 5:
        evaporation = 0.05
        stage2_days = 0

    elif surface_moisture > STAGE1_THRESHOLD:
        # Stage 1: испарение ограничено атмосферой (Penman-Monteith).
        # ETO_FACTOR объединяет: лесной полог (0.35) × аспект (0.6–1.2).
        # Ветер уже в ETO — двойной счёт не нужен.
        evaporation = eto * 0.9 * ETO_FACTOR
        stage2_days = 0

    else:
        # Stage 2: испарение ограничено почвенной диффузией (Philip 1957)
        # dE(t) = S × (√t − √(t−1)), t — реальные сутки в Stage 2
        stage2_days += 1
        str_factor  = math.sqrt(stage2_days) - math.sqrt(stage2_days - 1)
        evaporation = DESORPTIVITY * str_factor

        # Wind bonus Stage 2: ветер ускоряет конвективное испарение с влажной поверхности
        # только на открытых точках (не лес), только первые 5 суток (поверхность ещё влажная)
        # max +0.2 мм/день при ветре ≥8 м/с
        if not IS_FOREST and wind_speed is not None and wind_speed > 4.0 and stage2_days <= 5:
            wind_bonus  = 0.2 * min(1.0, (wind_speed - 4.0) / 4.0)
            evaporation = evaporation + wind_bonus

    evaporation       = min(evaporation, surface_moisture)
    surface_moisture  = surface_moisture + water_input - evaporation
    surface_moisture  = max(0, min(surface_moisture, CAPACITY))

    return surface_moisture, snow_cover, wet_index, stage2_days


def simulate_moisture(weather_data, soil_params):
    """Симуляция влажности поверхностного слоя по историческим данным."""
    daily = weather_data["daily"]
    surface_moisture = 0.0
    snow_cover = 0.0
    wet_index = 0.0
    stage2_days = 0

    winds = daily.get("wind_speed_10m_mean") or []

    for i in range(len(daily["time"])):
        wind_speed = (winds[i] if i < len(winds) else None) or None
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
            wind_speed=wind_speed,
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

    winds   = daily.get("wind_speed_10m_mean") or []
    results = []

    for i in range(len(daily["time"])):
        temp_mean  = ((daily["temperature_2m_max"][i] or 0) + (daily["temperature_2m_min"][i] or 0)) / 2
        wind_speed = (winds[i] if i < len(winds) else None) or None
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
            wind_speed=wind_speed,
        )
        results.append({
            "date": daily["time"][i],
            "moisture": surface_moisture,
            "capacity": soil_params["capacity"],
            "rain": daily["rain_sum"][i] or 0,
            "wet_index": wet_index,
            "snow_cover": snow_cover,
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


def fetch_surface_types_batch(lat_lon_pairs: list) -> list:
    """
    Один Overpass-запрос для всех точек разом (union around-queries).
    Возвращает список поверхностей в том же порядке, что и входные точки.
    """
    if not lat_lon_pairs:
        return []

    clauses = "".join(
        f"way(around:30,{lat},{lon})[highway];"
        for lat, lon in lat_lon_pairs
    )
    query = f"[out:json][timeout:90];({clauses});out body geom;"

    if not _overpass_wait_for_slot(timeout=90):
        logger.warning("fetch_surface_types_batch: no Overpass slot → all error")
        return ["error"] * len(lat_lon_pairs)

    elements = None
    for attempt in range(3):
        try:
            resp = requests.post(OVERPASS_URL, data={"data": query}, timeout=90)
        except Exception as e:
            logger.warning("fetch_surface_types_batch exception attempt %d: %s", attempt + 1, e)
            return ["error"] * len(lat_lon_pairs)

        if resp.status_code == 429:
            logger.warning("fetch_surface_types_batch: 429, waiting for slot...")
            if not _overpass_wait_for_slot(timeout=90):
                return ["error"] * len(lat_lon_pairs)
            continue

        if resp.status_code in (503, 504):
            wait = 5 * (attempt + 1)
            logger.warning("fetch_surface_types_batch HTTP %s, retry in %ds", resp.status_code, wait)
            time.sleep(wait)
            continue

        if resp.status_code != 200:
            logger.warning("fetch_surface_types_batch HTTP %s → all error", resp.status_code)
            return ["error"] * len(lat_lon_pairs)

        elements = resp.json().get("elements", [])
        break

    if elements is None:
        return ["error"] * len(lat_lon_pairs)

    results = [
        _find_surface_for_point(lat, lon, elements, radius_m=30)
        for lat, lon in lat_lon_pairs
    ]
    return results


def _find_surface_for_point(lat: float, lon: float, ways: list, radius_m: float) -> str:
    """Определяет тип покрытия для точки по предзагруженным OSM way-элементам с геометрией."""
    nearby = []
    for el in ways:
        geom = el.get("geometry", [])
        if not geom:
            continue
        min_dist_km = min(
            haversine_distance(lat, lon, node["lat"], node["lon"])
            for node in geom
        )
        if min_dist_km * 1000 <= radius_m:
            nearby.append((min_dist_km, el))

    if not nearby:
        logger.debug("_find_surface (%.5f, %.5f) no ways within %dm → ground", lat, lon, radius_m)
        return "ground"

    nearby.sort(key=lambda x: x[0])
    logger.debug("_find_surface (%.5f, %.5f) ways=%s", lat, lon,
                 [(el["tags"].get("highway"), el["tags"].get("surface")) for _, el in nearby])

    for _, el in nearby:
        if "surface" in el.get("tags", {}):
            result = el["tags"]["surface"]
            logger.info("_find_surface (%.5f, %.5f) surface_tag=%s", lat, lon, result)
            return result

    for _, el in nearby:
        hw = el.get("tags", {}).get("highway")
        if hw in _PAVED_HIGHWAY_TYPES:
            logger.info("_find_surface (%.5f, %.5f) highway=%s → asphalt", lat, lon, hw)
            return "asphalt"

    hw_types = [el.get("tags", {}).get("highway") for _, el in nearby]
    logger.info("_find_surface (%.5f, %.5f) highway=%s → ground", lat, lon, hw_types)
    return "ground"


def fetch_weather_data_batch(lat_lon_pairs: list, days_back: int = 14) -> list:
    """
    Open-Meteo archive API: один запрос для всех точек (CSV координаты).
    Возвращает список weather-dict'ов в том же порядке.
    """
    if not lat_lon_pairs:
        return []

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": ",".join(str(lat) for lat, lon in lat_lon_pairs),
        "longitude": ",".join(str(lon) for lat, lon in lat_lon_pairs),
        "start_date": start_date,
        "end_date": end_date,
        "daily": ",".join([
            "temperature_2m_mean",
            "rain_sum",
            "snowfall_sum",
            "et0_fao_evapotranspiration",
            "wind_speed_10m_mean",
        ]),
        "timezone": "auto",
    }

    data = _http_get_retry(url, params=params, timeout=60).json()
    return data if isinstance(data, list) else [data]


def fetch_forecast_batch(lat_lon_pairs: list, days_ahead: int = 16) -> list:
    """
    Open-Meteo forecast API: один запрос для всех точек (CSV координаты).
    Возвращает список forecast-dict'ов в том же порядке.
    """
    if not lat_lon_pairs:
        return []

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": ",".join(str(lat) for lat, lon in lat_lon_pairs),
        "longitude": ",".join(str(lon) for lat, lon in lat_lon_pairs),
        "daily": ",".join([
            "temperature_2m_max",
            "temperature_2m_min",
            "rain_sum",
            "snowfall_sum",
            "et0_fao_evapotranspiration",
            "wind_speed_10m_mean",
        ]),
        "forecast_days": days_ahead,
        "timezone": "auto",
    }

    data = _http_get_retry(url, params=params, timeout=60).json()
    return data if isinstance(data, list) else [data]


def fetch_surface_type(lat: float, lon: float) -> str:
    """
    Тип покрытия OSM для точки.
    Возвращает OSM surface tag, 'asphalt' для дорожных highway без surface тега,
    'ground' если данных нет, 'error' если Overpass недоступен.
    """
    query = (
        f"[out:json][timeout:10];"
        f"way(around:30,{lat},{lon})"
        f"[highway];"
        f"out tags;"
    )
    # Ждём доступного слота перед запросом
    if not _overpass_wait_for_slot():
        logger.warning("surface_type: Overpass no slot available at (%.5f, %.5f) → error", lat, lon)
        return "error"
    for attempt in range(3):
        try:
            resp = requests.post(OVERPASS_URL, data={"data": query}, timeout=12)
        except Exception as e:
            logger.warning("surface_type exception at (%.5f, %.5f): %s", lat, lon, e)
            return "error"

        if resp.status_code == 429:
            # Слот пропал между проверкой и запросом — ждём и делаем ещё одну попытку
            logger.warning("surface_type: unexpected 429 at (%.5f, %.5f), waiting for slot...", lat, lon)
            if not _overpass_wait_for_slot():
                return "error"
            continue  # повтор с тем же слотом

        if resp.status_code in (503, 504):
            # Сервер перегружен — короткая пауза и повтор
            wait = 5 * (attempt + 1)
            logger.warning("surface_type OSM HTTP %s at (%.5f, %.5f), retry in %ds (attempt %d/3)",
                           resp.status_code, lat, lon, wait, attempt + 1)
            time.sleep(wait)
            continue

        if resp.status_code != 200:
            logger.warning("surface_type OSM HTTP %s at (%.5f, %.5f) → error", resp.status_code, lat, lon)
            return "error"
        elements = resp.json().get("elements", [])
        if not elements:
            logger.debug("surface_type no ways at (%.5f, %.5f) → ground", lat, lon)
            return "ground"
        logger.debug("surface_type (%.5f, %.5f) ways=%s", lat, lon,
                     [(el["tags"].get("highway"), el["tags"].get("surface")) for el in elements])
        for el in elements:
            if "surface" in el.get("tags", {}):
                result = el["tags"]["surface"]
                logger.info("surface_type (%.5f, %.5f) surface_tag=%s", lat, lon, result)
                return result
        for el in elements:
            hw = el.get("tags", {}).get("highway")
            if hw in _PAVED_HIGHWAY_TYPES:
                logger.info("surface_type (%.5f, %.5f) no surface tag, highway=%s → asphalt", lat, lon, hw)
                return "asphalt"
        hw_types = [el.get("tags", {}).get("highway") for el in elements]
        logger.info("surface_type (%.5f, %.5f) highway=%s, no surface tag → ground", lat, lon, hw_types)
        return "ground"
    logger.warning("surface_type all retries failed at (%.5f, %.5f) → error", lat, lon)
    return "error"


def apply_surface_modifiers(soil_params: dict, surface: str,
                            is_forest: bool = False,
                            slope_deg: float = 0.0,
                            aspect_deg: float = None,
                            terrain_slope_deg: float = None) -> dict:
    """
    Возвращает копию soil_params, скорректированную под тип покрытия,
    растительный полог, уклон и аспект склона.

    is_forest — точка находится в лесу (OSM natural=wood / landuse=forest).
    Источники: Horton (1919), Zinke (1967), Oke (1987):
      - desorptivity ×0.5:  Stage 2 под пологом сохнет вдвое медленнее
      - eto_factor (лес) = 0.35: 65% инсоляции блокируется кронами
      - rain_factor = 0.80: 20% осадков перехватывается

    slope_deg — угол уклона трейла в градусах.
    Крутой склон дренирует быстрее → ёмкость ниже:
      slope_drainage = 1 + sin(slope) × 1.5
    slope_deg=15° → /1.39, slope_deg=30° → /1.75

    aspect_deg — аспект склона, градусы от севера по часовой стрелке (из DEM).
    None = плоский рельеф или аспект неизвестен → нейтральные коэффициенты.

    Формулы аспект-коэффициентов (непрерывные косинусные, без бинарных порогов):

      eto_aspect   = 1.0 − 0.4 × cos(aspect_rad)
        N (0°)  → 0.60  (меньше прямого солнца)
        S (180°)→ 1.40  → обрезаем до 1.25 (реальное превышение над горизонтальным ETO)
        E/W     → 1.00

      degree_day = 3.0 − 1.5 × cos(aspect_rad)
        N (0°)  → 1.50 мм/°С/день  (снег тает медленнее)
        S (180°)→ 4.50 → обрезаем до 4.00
        E/W     → 3.00
        Источник: Rango & Martinec (1995)

    terrain_slope_deg — уклон рельефа из DEM (возвращается fetch_aspect_batch вместе с
    аспектом). Используется как вес аспект-коэффициентов вместо along-track slope_deg,
    потому что трек может идти по горизонтальному контуру (slope_deg=0°) при явном
    аспекте склона — в таком случае along-track slope подавлял бы эффект аспекта.
    Если terrain_slope_deg не задан — фолбэк на slope_deg.
    """
    mods = SURFACE_SOIL_MODIFIERS.get(surface, {"capacity_mult": 1.0, "desorptivity_mult": 1.0, "snow_factor": 1.0})

    # Уклон → дренаж → ёмкость (вдоль трека, из GPX)
    slope_drainage = 1.0 + math.sin(math.radians(max(0.0, slope_deg))) * 1.5

    # Для аспект-веса используем terrain_slope_deg (DEM) если доступен,
    # иначе along-track slope_deg (GPX)
    weight_slope = terrain_slope_deg if terrain_slope_deg is not None else slope_deg

    # Аспект-коэффициенты (непрерывная косинусная формула)
    if aspect_deg is not None:
        # Плавное затухание на равнине: DEM slope=0° → weight=0, slope≥5° → weight=1
        aspect_weight = min(1.0, weight_slope / 5.0)
        cos_a         = math.cos(math.radians(aspect_deg))
        eto_aspect    = 1.0 + aspect_weight * (-0.4 * cos_a)   # 0.6 .. 1.4
        ddf_aspect    = 3.0 + aspect_weight * (-1.5 * cos_a)   # 1.5 .. 4.5
        eto_aspect    = min(1.25, max(0.60, eto_aspect))
        ddf_aspect    = min(4.00, max(1.50, ddf_aspect))
    else:
        eto_aspect = 1.0
        ddf_aspect = 3.0

    # Лесной полог
    if is_forest:
        eto_forest   = 0.35
        rain_factor  = 0.80
        desorpt_mult = 0.5
    else:
        eto_forest   = 1.0
        rain_factor  = 1.0
        desorpt_mult = 1.0

    # Нижний лимит eto_factor: даже в самых неблагоприятных условиях
    # (плотный полог + северный склон) испарение не обнуляется полностью
    combined_eto = max(0.15, eto_forest * eto_aspect)

    result = {
        **soil_params,
        "capacity":          soil_params["capacity"]     * mods["capacity_mult"] / slope_drainage,
        "desorptivity":      soil_params["desorptivity"] * mods["desorptivity_mult"] * desorpt_mult,
        "snow_factor":       mods.get("snow_factor", 1.0),
        "eto_factor":        combined_eto,   # лес × аспект, ≥ 0.15
        "rain_factor":       rain_factor,
        "degree_day_factor": ddf_aspect,
        "is_forest":         is_forest,
    }

    return result


def _point_in_polygon(lat: float, lon: float, nodes: list) -> bool:
    """
    Ray-casting алгоритм проверки точки в полигоне.
    nodes — список {'lat': ..., 'lon': ...} из геометрии OSM way.
    """
    inside = False
    n = len(nodes)
    j = n - 1
    for i in range(n):
        yi, xi = nodes[i]["lat"], nodes[i]["lon"]
        yj, xj = nodes[j]["lat"], nodes[j]["lon"]
        dy = yj - yi
        if dy == 0:
            j = i
            continue
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / dy + xi):
            inside = not inside
        j = i
    return inside


def fetch_surface_and_forest_batch(lat_lon_pairs: list):
    """
    ОДИН Overpass-запрос для всех точек: поверхности (highway around:30) +
    лесной полог (natural=wood / landuse=forest по bbox маршрута).

    Возвращает (surfaces: List[str], forest_flags: List[bool]) в том же порядке.

    Объединение двух запросов в один снижает нагрузку на Overpass вдвое —
    критично для /batch где 6 маршрутов × 2 запроса = 12 вызовов → rate limit.
    С объединённым запросом: 6 вызовов → вписываемся в лимит.
    """
    if not lat_lon_pairs:
        return [], []

    lats = [lat for lat, lon in lat_lon_pairs]
    lons = [lon for lat, lon in lat_lon_pairs]
    bbox = f"{min(lats)-0.01},{min(lons)-0.01},{max(lats)+0.01},{max(lons)+0.01}"

    # Секция highway: around:30 для каждой точки (как в fetch_surface_types_batch)
    highway_clauses = "".join(
        f"way(around:30,{lat},{lon})[highway];"
        for lat, lon in lat_lon_pairs
    )

    # Объединённый запрос:
    # .hways — highway ways для определения поверхности
    # .forest_rels — лесные relation (крупные массивы через relations)
    # .fways — forest ways (прямые + member ways из relations)
    # Финальный union выдаёт оба набора в одном ответе
    query = f"""[out:json][timeout:120];
({highway_clauses})->.hways;
(
  relation[natural=wood]({bbox});
  relation[landuse=forest]({bbox});
)->.forest_rels;
(
  way[natural=wood]({bbox});
  way[landuse=forest]({bbox});
  way(r.forest_rels);
)->.fways;
(.hways; .fways;);
out body geom;"""

    if not _overpass_wait_for_slot(timeout=120):
        logger.warning("fetch_surface_and_forest_batch: no Overpass slot → error/False")
        return ["error"] * len(lat_lon_pairs), [False] * len(lat_lon_pairs)

    elements = None
    for attempt in range(5):
        try:
            resp = requests.post(OVERPASS_URL, data={"data": query}, timeout=120)
        except Exception as e:
            delay = min(5 * (attempt + 1), 60)
            logger.warning("fetch_surface_and_forest_batch exception attempt %d: %s, retry in %ds",
                           attempt + 1, e, delay)
            time.sleep(delay)
            continue

        if resp.status_code == 429:
            if not _overpass_wait_for_slot(timeout=120):
                return ["error"] * len(lat_lon_pairs), [False] * len(lat_lon_pairs)
            continue
        if resp.status_code in (503, 504):
            delay = min(5 * (attempt + 1), 60)
            logger.warning("fetch_surface_and_forest_batch HTTP %s, retry in %ds", resp.status_code, delay)
            time.sleep(delay)
            continue
        if resp.status_code != 200:
            logger.warning("fetch_surface_and_forest_batch HTTP %s → error/False", resp.status_code)
            return ["error"] * len(lat_lon_pairs), [False] * len(lat_lon_pairs)

        elements = resp.json().get("elements", [])
        break

    if elements is None:
        return ["error"] * len(lat_lon_pairs), [False] * len(lat_lon_pairs)

    # Разделяем по наличию тега highway
    highway_elements = [el for el in elements if "highway" in el.get("tags", {})]
    forest_elements  = [el for el in elements if "highway" not in el.get("tags", {})]

    # Поверхности: используем уже существующую логику _find_surface_for_point
    surfaces = [
        _find_surface_for_point(lat, lon, highway_elements, radius_m=30)
        for lat, lon in lat_lon_pairs
    ]

    # Лесной полог: полигоны из forest_elements (минимум 3 узла)
    polygons = [el["geometry"] for el in forest_elements if len(el.get("geometry", [])) >= 3]
    if polygons:
        forest_flags = [
            any(_point_in_polygon(lat, lon, poly) for poly in polygons)
            for lat, lon in lat_lon_pairs
        ]
        logger.info(
            "fetch_surface_and_forest_batch: %d highway ways, %d forest polygons, "
            "%d/%d points in forest",
            len(highway_elements), len(polygons), sum(forest_flags), len(lat_lon_pairs),
        )
    else:
        forest_flags = [False] * len(lat_lon_pairs)
        logger.info(
            "fetch_surface_and_forest_batch: %d highway ways, no forest polygons in bbox",
            len(highway_elements),
        )

    return surfaces, forest_flags


def fetch_forest_flags_batch(lat_lon_pairs: list) -> list:
    """
    Проверяет наличие лесного полога (OSM natural=wood / landuse=forest)
    для каждой точки маршрута. Один батч-запрос по bbox всего маршрута.

    Возвращает список bool в том же порядке, что и входные точки.
    При ошибке Overpass возвращает all-False (без лесных модификаторов),
    что не ломает симуляцию.
    """
    if not lat_lon_pairs:
        return []

    lats = [lat for lat, lon in lat_lon_pairs]
    lons = [lon for lat, lon in lat_lon_pairs]
    # bbox с небольшим отступом для захвата граничных полигонов
    bbox = f"{min(lats)-0.01},{min(lons)-0.01},{max(lats)+0.01},{max(lons)+0.01}"

    # Запрашиваем:
    # 1. Напрямую tagged ways (небольшие лесополосы, городские леса)
    # 2. Relations — крупные лесные массивы (Россия, Скандинавия, Беларусь)
    #    → рекурсивно разворачиваем в member ways для получения геометрии
    query = f"""[out:json][timeout:90];
(
  relation[natural=wood]({bbox});
  relation[landuse=forest]({bbox});
)->.forest_rels;
(
  way[natural=wood]({bbox});
  way[landuse=forest]({bbox});
  way(r.forest_rels);
);
out body geom;"""

    if not _overpass_wait_for_slot(timeout=90):
        logger.warning("fetch_forest_flags_batch: no Overpass slot → all False")
        return [False] * len(lat_lon_pairs)

    elements = None
    for attempt in range(5):
        try:
            resp = requests.post(OVERPASS_URL, data={"data": query}, timeout=90)
        except Exception as e:
            delay = min(5 * (attempt + 1), 30)
            logger.warning("fetch_forest_flags_batch exception attempt %d: %s, retry in %ds",
                           attempt + 1, e, delay)
            time.sleep(delay)
            continue

        if resp.status_code == 429:
            if not _overpass_wait_for_slot(timeout=90):
                return [False] * len(lat_lon_pairs)
            continue
        if resp.status_code in (503, 504):
            delay = min(5 * (attempt + 1), 60)
            logger.warning("fetch_forest_flags_batch HTTP %s, retry in %ds", resp.status_code, delay)
            time.sleep(delay)
            continue
        if resp.status_code != 200:
            logger.warning("fetch_forest_flags_batch HTTP %s → all False", resp.status_code)
            return [False] * len(lat_lon_pairs)

        elements = resp.json().get("elements", [])
        break

    if elements is None:
        return [False] * len(lat_lon_pairs)

    # Собираем замкнутые полигоны из way-элементов (минимум 3 узла)
    polygons = [el["geometry"] for el in elements if len(el.get("geometry", [])) >= 3]
    if not polygons:
        logger.info("fetch_forest_flags_batch: no forest polygons in bbox")
        return [False] * len(lat_lon_pairs)

    flags = [
        any(_point_in_polygon(lat, lon, poly) for poly in polygons)
        for lat, lon in lat_lon_pairs
    ]
    logger.info(
        "fetch_forest_flags_batch: %d polygons, %d/%d points in forest",
        len(polygons), sum(flags), len(lat_lon_pairs),
    )
    return flags


def compute_slopes_for_sampled(all_gpx_points: list, sampled_points: list) -> list:
    """
    Вычисляет угол уклона (градусы) для каждой точки из sampled_points
    по профилю высот трека в окне ±500м вдоль пути.

    all_gpx_points — [(lat, lon, elev), ...]
    sampled_points — [(lat, lon, elev, dist_km), ...]
    Возвращает [slope_deg, ...] в том же порядке.

    Используется как входной параметр для apply_surface_modifiers:
    крутой склон → быстрый дренаж → меньшая эффективная ёмкость.
    """
    if not all_gpx_points or not sampled_points:
        return [0.0] * len(sampled_points)

    # Строим кумулятивные расстояния по полному треку
    cum_dists = [0.0]
    for i in range(1, len(all_gpx_points)):
        d = haversine_distance(
            all_gpx_points[i-1][0], all_gpx_points[i-1][1],
            all_gpx_points[i][0],   all_gpx_points[i][1],
        )
        cum_dists.append(cum_dists[-1] + d)

    slopes = []
    for _, _, _, s_dist_km in sampled_points:
        # Собираем точки трека в окне ±0.5 км от текущей позиции
        window = [
            (cum_dists[i], all_gpx_points[i][2])
            for i in range(len(all_gpx_points))
            if abs(cum_dists[i] - s_dist_km) <= 0.5
            and all_gpx_points[i][2] is not None
            and all_gpx_points[i][2] > 0
        ]

        if len(window) < 2:
            slopes.append(0.0)
            continue

        window.sort()
        elevs = [e for _, e in window]
        dists  = [d for d, _ in window]
        total_dist_km = dists[-1] - dists[0]

        if total_dist_km < 0.01:
            slopes.append(0.0)
            continue

        # Суммарный абсолютный набор высоты на окне (не чистый перепад)
        total_elev_change = sum(abs(elevs[k+1] - elevs[k]) for k in range(len(elevs) - 1))
        slope_ratio = total_elev_change / (total_dist_km * 1000.0)  # м/м
        slope_angle = math.degrees(math.atan(slope_ratio))
        slopes.append(round(slope_angle, 1))

    return slopes


# URL бесплатного DEM-сервиса на базе SRTM 90м (без ключа, без лимитов)
OPEN_ELEVATION_URL = "https://api.open-elevation.com/api/v1/lookup"


def fetch_aspect_batch(lat_lon_pairs: list) -> list:
    """
    Вычисляет аспект и уклон рельефа (не трека) для каждой точки по SRTM DEM 90м
    через Open-Elevation API (бесплатно, без ключа).

    Для каждой точки запрашивает 3 высоты: center, north +100м, east +100м.
    LON_OFFSET корректируется по широте: lon_offset = lat_offset / cos(lat),
    чтобы оба смещения были физически ~100м независимо от широты.

    Из градиента высот:
      dz/dy = (elev_N − elev_C) / 100   [м/м, направление север]
      dz/dx = (elev_E − elev_C) / 100   [м/м, направление восток]
      aspect         = atan2(−dz/dx, −dz/dy) mod 360   [°, направление «вниз»]
      terrain_slope  = degrees(atan(sqrt(dz_dy²+dz_dx²)))  [°, крутизна рельефа]

    Возвращает List[Optional[Tuple[float, float]]]:
      (aspect_deg, terrain_slope_deg) — или None для плоского/недоступного рельефа.

    terrain_slope_deg используется в apply_surface_modifiers как weight для
    аспект-коэффициентов (вместо GPX along-track slope, который равен 0 при
    траверсе горизонтального контура).

    При ошибке API — all-None (→ нейтральные коэффициенты, не ломает симуляцию).
    """
    if not lat_lon_pairs:
        return []

    LAT_OFFSET = 0.0009   # ≈ 100м по широте (константа)

    locations = []
    for lat, lon in lat_lon_pairs:
        # LON_OFFSET пересчитывается для каждой точки под фактическую широту
        lon_offset = LAT_OFFSET / max(0.01, math.cos(math.radians(lat)))
        locations.append({"latitude": lat,              "longitude": lon})
        locations.append({"latitude": lat + LAT_OFFSET, "longitude": lon})
        locations.append({"latitude": lat,              "longitude": lon + lon_offset})

    try:
        resp = _http_post_retry(OPEN_ELEVATION_URL, json={"locations": locations}, timeout=30)
    except Exception as e:
        logger.warning("fetch_aspect_batch: все попытки провалились: %s", e)
        return [None] * len(lat_lon_pairs)

    raw = resp.json().get("results", [])
    if len(raw) != len(locations):
        logger.warning("fetch_aspect_batch: ожидали %d результатов, получили %d",
                       len(locations), len(raw))
        return [None] * len(lat_lon_pairs)

    results = []
    for i, (lat, lon) in enumerate(lat_lon_pairs):
        ec = raw[i * 3]["elevation"]
        en = raw[i * 3 + 1]["elevation"]
        ee = raw[i * 3 + 2]["elevation"]

        if ec is None or en is None or ee is None:
            results.append(None)
            continue

        lon_offset = LAT_OFFSET / max(0.01, math.cos(math.radians(lat)))
        horiz_m    = lon_offset * 111320  # метров на 1° долготы × смещение в °

        dz_dy = (en - ec) / 100.0         # м/м на север (100м фиксировано)
        dz_dx = (ee - ec) / horiz_m       # м/м на восток (реальное расстояние)

        gradient_mag = math.sqrt(dz_dy ** 2 + dz_dx ** 2)

        if gradient_mag < 1e-4:
            results.append(None)   # плоский рельеф
            continue

        aspect        = math.degrees(math.atan2(-dz_dx, -dz_dy)) % 360
        terrain_slope = math.degrees(math.atan(gradient_mag))
        results.append((round(aspect, 1), round(terrain_slope, 1)))

    n_valid = sum(1 for r in results if r is not None)
    logger.info("fetch_aspect_batch: %d/%d valid (aspect, slope)", n_valid, len(lat_lon_pairs))
    return results


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

    lat_lon_pairs = [(lat, lon) for lat, lon, elev, dist_km in sampled]

    if verbose:
        print(f"   Запрашиваю покрытия и лесной полог для {len(sampled)} точек (batch Overpass)...")
    surfaces, forest_flags = fetch_surface_and_forest_batch(lat_lon_pairs)

    if verbose:
        print(f"   Запрашиваю аспект склонов (SRTM DEM)...")
    aspect_results = fetch_aspect_batch(lat_lon_pairs)

    if verbose:
        print(f"   Запрашиваю погоду для {len(sampled)} точек (batch Open-Meteo)...")
    weather_batch = fetch_weather_data_batch(lat_lon_pairs, days_back=14)

    # Уклон из GPX-профиля
    slope_degs = compute_slopes_for_sampled(points, sampled)

    if verbose:
        print()

    # Анализ каждой точки (чистые вычисления, без сетевых вызовов)
    results = []
    for idx, (lat, lon, elev, dist_km) in enumerate(sampled):
        if verbose:
            print(f"   [{idx+1}/{len(sampled)}] км {dist_km:.1f}: ({lat:.4f}, {lon:.4f})...", end=" ", flush=True)

        try:
            surface   = surfaces[idx]
            weather   = weather_batch[idx]
            if weather is None:
                raise Exception("нет данных погоды")
            is_forest  = forest_flags[idx]  if idx < len(forest_flags)  else False
            slope_deg  = slope_degs[idx]    if idx < len(slope_degs)    else 0.0
            dem        = aspect_results[idx] if idx < len(aspect_results) else None
            aspect_deg, terrain_slope_deg = dem if dem is not None else (None, None)

            soil_params = apply_surface_modifiers(
                SOIL_PARAMS, surface,
                is_forest=is_forest,
                slope_deg=slope_deg,
                aspect_deg=aspect_deg,
                terrain_slope_deg=terrain_slope_deg,
            )
            state = simulate_moisture(weather, soil_params)
            status_label, status_key = get_status(state["moisture"], state["capacity"])

            results.append({
                "lat": lat, "lon": lon, "elevation": elev,
                "distance_km": dist_km,
                "surface":     surface,
                "is_forest":   is_forest,
                "slope_deg":   slope_deg,
                "aspect_deg":  aspect_deg,
                "moisture":    state["moisture"],
                "capacity":    state["capacity"],
                "wet_index":   state["wet_index"],
                "snow_cover":  state["snow_cover"],
                "stage2_days": state["stage2_days"],
                "status_label": status_label,
                "status_key":   status_key,
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

    forecast_points = valid
    
    if verbose:
        print(f"\n🔮 Прогноз высыхания по {len(forecast_points)} точкам...")
    
    all_forecasts = []

    # Батч-запрос прогноза для всех точек — один вызов API вместо N
    lat_lon_pairs = [(p["lat"], p["lon"]) for p in forecast_points]
    try:
        forecasts_data = fetch_forecast_batch(lat_lon_pairs, days_ahead=16)
    except Exception as e:
        if verbose:
            print(f"   ⚠️ Ошибка батч-прогноза: {e}")
        forecasts_data = [None] * len(forecast_points)

    for idx, (point, forecast) in enumerate(zip(forecast_points, forecasts_data)):
        if forecast is None:
            if verbose:
                print(f"   [{idx+1}/{len(forecast_points)}] км {point['distance_km']:.0f} ⚠️ нет данных")
            continue
        try:
            initial_state = {
                "moisture":    point["moisture"],
                "capacity":    point["capacity"],
                "wet_index":   point["wet_index"],
                "snow_cover":  point["snow_cover"],
                "stage2_days": point.get("stage2_days", 0),
            }
            soil_params = apply_surface_modifiers(
                SOIL_PARAMS,
                point.get("surface", "ground"),
                is_forest=point.get("is_forest", False),
                slope_deg=point.get("slope_deg", 0.0),
                aspect_deg=point.get("aspect_deg"),
            )
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
