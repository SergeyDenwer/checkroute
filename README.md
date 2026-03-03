# 🛤 CheckRoute

Telegram-бот для проверки состояния грунтовых маршрутов. Загрузи GPX — узнай, можно ли катать и когда высохнет.

![Python](https://img.shields.io/badge/python-3.9+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

## Что делает

- Анализирует GPX-маршрут по контрольным точкам (каждые 5 км)
- Получает историю погоды за 14 дней для каждой точки
- Моделирует влажность поверхностного слоя почвы
- Показывает текущее состояние и прогноз высыхания на 16 дней

## Пример отчёта

```
🛤 CheckRoute
📏 154.8 км | 🌍 Суглинок (loam)

📊 СОСТОЯНИЕ:
☀️ СУХО      ░░░░░░░░░░░░░░░░░░░░   0%
🟠 ВЛАЖНО    ░░░░░░░░░░░░░░░░░░░░   0%
🔴 ГРЯЗЬ     ░░░░░░░░░░░░░░░░░░░░   0%
💀 МЕСИВО    ████████████████████ 100%

🎯 🔴 НЕЛЬЗЯ
Оно тебя сожрёт. Сиди дома

🔮 ПРОГНОЗ:
Дата    Сухо Влаж Гряз Меси
03-03    0%   0%  57%  43%
03-04    0%   0%  86%  14%
03-05    0%  43%  57%   0%
...

🔴 НЕЛЬЗЯ: сегодня
🟠 СКОРЕЕ НЕЛЬЗЯ: 2026-03-06 (через 3 дн)
🟢 СКОРЕЕ МОЖНО: 2026-03-11 (через 8 дн)
✅ МОЖНО: 2026-03-13 (через 10 дн)
```

## Установка

```bash
# Клонируй репозиторий
git clone https://github.com/SergeyDenwer/checkroute.git
cd checkroute

# Создай виртуальное окружение
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# или venv\Scripts\activate  # Windows

# Установи зависимости
pip install -r requirements.txt

# Скопируй конфиг
cp .env.example .env
# Отредактируй .env — вставь токен бота
```

## Запуск

```bash
# Активируй окружение
source venv/bin/activate

# Запусти
python checkroute_bot.py
```

## Получение токена

1. Напиши [@BotFather](https://t.me/BotFather) в Telegram
2. Отправь `/newbot`
3. Выбери имя (например, "CheckRoute")
4. Выбери username (например, "my_checkroute_bot")
5. Скопируй токен в `.env`

## Автозапуск на Mac

Создай `~/Library/LaunchAgents/com.checkroute.bot.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.checkroute.bot</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/YOUR_USER/checkroute/run.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/YOUR_USER/checkroute/bot.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOUR_USER/checkroute/bot.log</string>
</dict>
</plist>
```

Создай `run.sh`:

```bash
#!/bin/bash
cd ~/checkroute
source venv/bin/activate
export $(cat .env | xargs)
exec python checkroute_bot.py
```

Запусти:

```bash
chmod +x run.sh
launchctl load ~/Library/LaunchAgents/com.checkroute.bot.plist
```

## Команды бота

- `/start` — приветствие
- `/help` — справка
- `/soil <тип>` — выбрать тип почвы

**Типы почвы:**
- `sand` — песок (быстро сохнет)
- `sandy_loam` — супесь
- `loam` — суглинок (по умолчанию)
- `silt_loam` — пылеватый суглинок
- `clay_loam` — глинистый суглинок
- `clay` — глина (медленно сохнет)
- `chernozem` — чернозём

---

## Алгоритм

### Модель

Используется **двухстадийная модель испарения** из поверхностного слоя почвы (0-10 см), основанная на работах Ritchie (1972) и Stroosnijder (1987).

### Stage 1: Мокрая почва (>50% ёмкости)

Испарение ограничено только атмосферными условиями:

```
E = 0.9 × ETo
```

где ETo — референсная эвапотранспирация по FAO-56 Penman-Monteith.

### Stage 2: Подсыхание (<50% ёмкости)

Испарение лимитируется почвой, замедляется по формуле Stroosnijder:

```
E(t) = D × (√t - √(t-1))
```

где:
- `D` — desorptivity (параметр почвы, мм/√день)
- `t` — дни с последнего значимого дождя (>3 мм)

Кумулятивное испарение за t дней: `ΣE = D × √t`

### Параметры почвы

| Тип почвы | Desorptivity (D) | Ёмкость слоя | Скорость высыхания |
|-----------|------------------|--------------|-------------------|
| Песок | 4.5 мм/√день | 10 мм | Быстро |
| Супесь | 4.0 мм/√день | 12 мм | Быстро |
| Суглинок | 3.5 мм/√день | 15 мм | Средне |
| Пылеватый суглинок | 3.2 мм/√день | 16 мм | Средне |
| Глинистый суглинок | 3.0 мм/√день | 18 мм | Медленно |
| Глина | 2.5 мм/√день | 20 мм | Медленно |
| Чернозём | 3.0 мм/√день | 18 мм | Средне |

### Пороги состояния

Относительно ёмкости слоя:

| Состояние | Влажность | Описание |
|-----------|-----------|----------|
| ☀️ СУХО | <20% | Отлично, катай |
| 🟠 ВЛАЖНО | 20-45% | Скользко местами |
| 🔴 ГРЯЗЬ | 45-75% | Грязно |
| 💀 МЕСИВО | >75% | Жопа |

### Вердикты

| Вердикт | Условие |
|---------|---------|
| ✅ МОЖНО | ≥70% точек "сухо" |
| 🟢 СКОРЕЕ МОЖНО | ≥50% "сухо" ИЛИ (≥80% "сухо"+"влажно" И ≥30% "сухо") |
| 🟠 СКОРЕЕ НЕЛЬЗЯ | ≥50% "сухо"+"влажно", но не дотягивает |
| 🔴 НЕЛЬЗЯ | >50% "грязь"+"месиво" |

### Источники данных

- **Погода:** [Open-Meteo API](https://open-meteo.com/) (история + прогноз)
- **ETo:** FAO-56 Penman-Monteith (рассчитывается Open-Meteo)

### Литература

1. Ritchie, J.T. (1972). Model for predicting evaporation from a row crop with incomplete cover. *Water Resources Research*, 8(5), 1204-1213.

2. Stroosnijder, L. (1987). Soil evaporation: test of a practical approach under semi-arid conditions. *Netherlands Journal of Agricultural Science*, 35, 417-426.

3. Allen, R.G. et al. (1998). Crop evapotranspiration: Guidelines for computing crop water requirements. *FAO Irrigation and Drainage Paper 56*. [PDF](https://www.fao.org/3/x0490e/x0490e00.htm)

4. LISFLOOD Model Documentation. [Link](https://ec-jrc.github.io/lisflood-model/)

---

## Структура проекта

```
checkroute/
├── checkroute_bot.py    # Telegram бот
├── trail_moisture_v4.py # Логика расчёта
├── requirements.txt     # Зависимости
├── .env.example         # Пример конфига
├── .env                 # Конфиг (не в git!)
├── run.sh              # Скрипт запуска
└── README.md
```

## Лицензия

MIT

## TODO

- [ ] Учёт высоты (быстрее сохнет в горах)
- [ ] Учёт экспозиции склона (юг/север)
- [ ] Кэширование погоды для близких точек
- [ ] Web-интерфейс
