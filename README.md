"""
Forex Price-Action Signal Bot
------------------------------
Логіка:
  1. 30m графік: тренд визначається за EMA200 (ціна вище -> BUY, нижче -> SELL).
  2. 5m графік: знаходиться останній імпульс (swing high/low), рахуються рівні
     Фібоначчі відкату (38.2 / 50 / 61.8%).
  3. POC (Point of Control) рахується без реального обсягу (на форексі його
     немає) — ціновий діапазон імпульсу ділиться на 20 зон, і зоною POC
     вважається та, де ціна (за хвостами свічок) "затрималась" найдовше
     (найбільше touch count).
  4. Сигнал формується, якщо поточна ціна одночасно:
       - близько до одного з рівнів Фібо (в межах TOLERANCE_PCT)
       - близько до POC (в межах TOLERANCE_PCT)
     і напрямок збігається з трендом 30m.
  5. Надсилається HTML-повідомлення в Telegram.

Запуск: розрахований на одноразовий виклик за розкладом (GitHub Actions cron).
Ніякого `while True` — плануванням займається сам GitHub Actions.
Дедуп (щоб не спамити тим самим сигналом) робиться через файл last_signals.json.
"""

import json
import os
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
import requests

# ---------------------------------------------------------------------------
# НАЛАШТУВАННЯ
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PASTE_YOUR_CHAT_ID_HERE")

PAIRS = [
    "AUDCAD=X", "AUDCHF=X", "AUDUSD=X", "AUDJPY=X",
    "CADCHF=X", "CADJPY=X", "CHFJPY=X",
    "EURAUD=X", "EURCAD=X", "EURCHF=X", "EURGBP=X", "EURJPY=X", "EURUSD=X",
    "GBPAUD=X", "GBPCAD=X", "GBPCHF=X", "GBPJPY=X", "GBPUSD=X",
    "USDCAD=X", "USDCHF=X", "USDJPY=X",
]

# Робоче вікно (за твоїм часовим поясом Europe/Kyiv). Поза цим вікном скрипт
# завершується одразу, не роблячи запитів.
TZ = ZoneInfo("Europe/Kyiv")
WORK_START = dtime(10, 0)
WORK_END = dtime(20, 0)

TREND_EMA_PERIOD = 200          # EMA на 30m для визначення тренду
POC_ZONES = 30                  # на скільки цінових зон ділимо діапазон
TOLERANCE_PCT = 0.00015         # ~0.015% — наскільки близько ціна має підійти (звужено)
IMPULSE_LOOKBACK_5M = 150       # ширша історія 5m, щоб ловити "старі" рівні
ZONE_MERGE_PCT = 0.0015         # зони ближче ніж 0.15% одна до одної зливаються
MIN_ZONE_TOUCHES = 4            # зона вважається рівнем, тільки якщо ціна
                                 # торкалась її мінімум стільки разів — слабші
                                 # зони (шум) до драбини входів не потрапляють
BREAKOUT_RANGE_MULT = 1.8       # свічка вважається "імпульсним пробоєм", якщо
                                 # її діапазон (High-Low) у стільки разів більший
                                 # за середній діапазон останніх свічок
BREAKOUT_LOOKBACK = 20          # на скількох останніх свічках рахуємо середній діапазон
# Час експірації по рівнях драбини входів (підтверджено скріншотами реальних
# угод трейдера): базовий вхід — 5 хв, кожне наступне перекриття — 10 хв.
LEVEL_EXPIRY_MIN = {1: 5, 2: 10, 3: 10}
LEVEL_LABELS = {1: "Основа", 2: "1-е перекриття", 3: "2-е перекриття (POC)"}

# --- Економічний календар (Forex Factory, безкоштовно, без ключа) ---
NEWS_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_BEFORE_MIN = 15            # за скільки хвилин ДО новини попереджати
NEWS_AFTER_MIN = 20             # скільки хвилин ПІСЛЯ новини ще не торгувати
# Валюти, які цікавлять (витягуються з тікерів пар автоматично, це просто мапа
# ISO-коду з календаря на позначення в тікерах yfinance)
WATCHED_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF"}

STATE_FILE = os.path.join(os.path.dirname(__file__), "last_signals.json")

# ---------------------------------------------------------------------------
# ДОПОМІЖНІ ФУНКЦІЇ
# ---------------------------------------------------------------------------


def in_trading_window() -> bool:
    now = datetime.now(TZ).time()
    return WORK_START <= now <= WORK_END


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def fetch_candles(pair: str, interval: str, period: str) -> pd.DataFrame:
    df = yf.download(pair, interval=interval, period=period, progress=False, auto_adjust=False)
    if df is None or df.empty:
        return pd.DataFrame()
    # yfinance інколи повертає мультиіндекс колонок — приводимо до простого вигляду
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna()
    return df


def get_trend_30m(pair: str) -> str | None:
    df = fetch_candles(pair, interval="30m", period="30d")
    if len(df) < TREND_EMA_PERIOD:
        return None
    df["EMA"] = df["Close"].ewm(span=TREND_EMA_PERIOD, adjust=False).mean()
    last_close = df["Close"].iloc[-1]
    last_ema = df["EMA"].iloc[-1]
    return "BUY" if last_close > last_ema else "SELL"


def find_last_impulse(df: pd.DataFrame):
    """Дуже спрощений пошук останнього імпульсу: бере діапазон
    high/low за останні IMPULSE_LOOKBACK_5M свічок."""
    window = df.tail(IMPULSE_LOOKBACK_5M)
    impulse_high = window["High"].max()
    impulse_low = window["Low"].min()
    return impulse_low, impulse_high, window


def compute_zone_touches(window: pd.DataFrame, low: float, high: float) -> list[tuple[float, int]]:
    """Ділить діапазон [low, high] на POC_ZONES смуг і рахує, скільки разів
    ціна (High-Low кожної свічки) торкалась кожної смуги.
    Повертає список (ціна_центру_зони, кількість_дотиків)."""
    if high <= low:
        return [((high + low) / 2, 1)]
    zone_size = (high - low) / POC_ZONES
    touches = [0] * POC_ZONES

    for _, row in window.iterrows():
        zone_lo = int((row["Low"] - low) / zone_size)
        zone_hi = int((row["High"] - low) / zone_size)
        zone_lo = max(0, min(POC_ZONES - 1, zone_lo))
        zone_hi = max(0, min(POC_ZONES - 1, zone_hi))
        for z in range(zone_lo, zone_hi + 1):
            touches[z] += 1

    zones = [(low + zone_size * (i + 0.5), touches[i]) for i in range(POC_ZONES) if touches[i] > 0]
    return zones


def merge_close_zones(zones: list[tuple[float, int]]) -> list[tuple[float, int]]:
    """Зливає сусідні цінові зони, якщо вони ближче ніж ZONE_MERGE_PCT одна
    до одної (щоб не рахувати один і той самий рівень кілька разів)."""
    if not zones:
        return []
    zones_sorted = sorted(zones, key=lambda z: z[0])
    merged = [zones_sorted[0]]
    for price, touches in zones_sorted[1:]:
        last_price, last_touches = merged[-1]
        if abs(price - last_price) / last_price <= ZONE_MERGE_PCT:
            # зливаємо: середньозважена ціна, сума дотиків
            total = last_touches + touches
            new_price = (last_price * last_touches + price * touches) / total
            merged[-1] = (new_price, total)
        else:
            merged.append((price, touches))
    return merged


def build_entry_ladder(window: pd.DataFrame, low: float, high: float, trend: str) -> list[dict]:
    """Будує драбину з до 3 рівнів входу за мотивами стратегії трейдера:
      - Рівень 3 = POC (зона з максимальною кількістю дотиків ціни).
      - Рівні 1 і 2 = наступні за силою зони, розташовані МІЖ поточним краєм
        імпульсу і POC, впорядковані від найближчої до найдальшої (тобто
        рівень 1 спрацьовує першим на неглибокому відкаті, рівень 2 — на
        глибшому, рівень 3 (POC) — останній, найглибший відкат)."""
    raw_zones = compute_zone_touches(window, low, high)
    zones = merge_close_zones(raw_zones)
    # відсікаємо слабкі зони — лишаємо тільки ті, де ціна дійсно "затримувалась"
    zones = [z for z in zones if z[1] >= MIN_ZONE_TOUCHES]
    if not zones:
        return []

    zones_sorted_by_strength = sorted(zones, key=lambda z: z[1], reverse=True)
    poc_price, poc_touches = zones_sorted_by_strength[0]

    # Точка відліку відкату: верх імпульсу для BUY (ціна відкочується вниз
    # до рівнів), низ імпульсу для SELL (ціна відкочується вгору).
    edge = high if trend == "BUY" else low

    other_zones = [z for z in zones_sorted_by_strength[1:] if z[0] != poc_price]
    # серед інших зон беремо ті, що лежать між edge і POC (на шляху відкату)
    if trend == "BUY":
        candidates = [z for z in other_zones if poc_price <= z[0] <= edge]
        candidates.sort(key=lambda z: -z[0])  # від найближчої до edge до найдальшої
    else:
        candidates = [z for z in other_zones if edge <= z[0] <= poc_price]
        candidates.sort(key=lambda z: z[0])

    level_prices = [z[0] for z in candidates[:2]]  # рівень 1, рівень 2
    level_prices.append(poc_price)                 # рівень 3 = POC

    ladder = []
    for idx, price in enumerate(level_prices, start=1):
        ladder.append({
            "level": idx,
            "price": price,
            "expiry_min": LEVEL_EXPIRY_MIN.get(idx, 5),
            "is_poc": idx == 3,
        })
    return ladder


def close_enough(price: float, target: float) -> bool:
    return abs(price - target) / target <= TOLERANCE_PCT


def is_impulsive_breakout(df5: pd.DataFrame, trend: str) -> bool:
    """Перевіряє, чи остання свічка (і при потребі одна-дві перед нею)
    'пробиває рівень як ніж масло': діапазон значно більший за середній,
    і закриття йде далеко за рівень без відбою (без довгої протилежної тіні).
    Якщо так — перекриття (1-е/2-е) не пропонуються, тільки базовий вхід
    або взагалі нічого, з рекомендацією фіксованої ставки."""
    recent = df5.tail(BREAKOUT_LOOKBACK)
    if len(recent) < 5:
        return False

    avg_range = (recent["High"] - recent["Low"]).mean()
    last = df5.iloc[-1]
    last_range = last["High"] - last["Low"]
    if avg_range <= 0 or last_range < avg_range * BREAKOUT_RANGE_MULT:
        return False

    body = abs(last["Close"] - last["Open"])
    # тіло свічки має займати більшість діапазону (мала відбійна тінь)
    if last_range <= 0 or body / last_range < 0.6:
        return False

    # напрямок свічки має збігатись з трендом (імпульс по тренду, не проти нього)
    candle_up = last["Close"] > last["Open"]
    if trend == "BUY" and not candle_up:
        return False
    if trend == "SELL" and candle_up:
        return False

    return True


def send_telegram_message(html_text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": html_text,
        "parse_mode": "HTML",
    }
    resp = requests.post(url, data=payload, timeout=15)
    if resp.status_code != 200:
        print(f"[Telegram error] {resp.status_code}: {resp.text}")


def format_signal(pair: str, trend: str, price: float, level: dict, breakout: bool) -> str:
    clean_pair = pair.replace("=X", "")
    direction = "🟢 BUY (CALL)" if trend == "BUY" else "🔴 SELL (PUT)"
    level_label = LEVEL_LABELS.get(level["level"], f"Рівень {level['level']}")
    warning = ""
    if breakout:
        warning = (
            "\n⚠️ Схоже на імпульсний пробій рівня — "
            "рекомендована ФІКСОВАНА ставка, без перекриттів."
        )
    return (
        f"<b>Сигнал: {clean_pair}</b>\n"
        f"Напрямок: {direction}\n"
        f"{level_label}\n"
        f"Ціна входу: <code>{price:.5f}</code>\n"
        f"Ціна рівня: <code>{level['price']:.5f}</code>\n"
        f"Експірація: {level['expiry_min']} хв"
        f"{warning}\n"
        f"Час: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')} (Kyiv)"
    )


# ---------------------------------------------------------------------------
# ЕКОНОМІЧНИЙ КАЛЕНДАР
# ---------------------------------------------------------------------------


def fetch_high_impact_events() -> list[dict]:
    """Тягне календар на поточний тиждень і лишає тільки High impact події."""
    try:
        resp = requests.get(NEWS_CALENDAR_URL, timeout=15)
        resp.raise_for_status()
        events = resp.json()
    except Exception as e:
        print(f"[Календар] не вдалось отримати дані: {e}")
        return []

    high_impact = []
    for ev in events:
        if ev.get("impact") != "High":
            continue
        if ev.get("country") not in WATCHED_CURRENCIES:
            continue
        try:
            ev_time = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(TZ)
        except Exception:
            continue
        high_impact.append({
            "title": ev.get("title", "Подія"),
            "currency": ev.get("country"),
            "time": ev_time,
        })
    return high_impact


def find_relevant_news_window(pair: str, events: list[dict]):
    """Повертає подію, якщо зараз потрапляємо у вікно 'до/після новини' для
    валют, що входять у дану пару."""
    clean = pair.replace("=X", "")
    pair_currencies = {clean[:3], clean[3:6]}
    now = datetime.now(TZ)

    for ev in events:
        if ev["currency"] not in pair_currencies:
            continue
        delta_min = (ev["time"] - now).total_seconds() / 60
        if -NEWS_AFTER_MIN <= delta_min <= NEWS_BEFORE_MIN:
            return ev
    return None


def maybe_send_news_warning(pair: str, event: dict, state: dict) -> None:
    clean_pair = pair.replace("=X", "")
    key = f"news_{clean_pair}_{event['title']}_{event['time'].strftime('%Y%m%d%H%M')}"
    if state.get(key):
        return  # вже попереджали про цю подію для цієї пари

    msg = (
        f"⚠️ <b>Скоро важлива новина: {clean_pair}</b>\n"
        f"Подія: {event['title']} ({event['currency']})\n"
        f"Час виходу: {event['time'].strftime('%H:%M')} (Kyiv)\n"
        f"Рекомендація: не торгувати ~{NEWS_BEFORE_MIN} хв до і "
        f"~{NEWS_AFTER_MIN} хв після виходу."
    )
    send_telegram_message(msg)
    state[key] = True
    print(f"{pair}: попередження про новину відправлено")


# ---------------------------------------------------------------------------
# ОСНОВНА ЛОГІКА ПО ОДНІЙ ПАРІ
# ---------------------------------------------------------------------------


def check_pair(pair: str, state: dict, news_events: list[dict]) -> None:
    news_hit = find_relevant_news_window(pair, news_events)
    if news_hit:
        maybe_send_news_warning(pair, news_hit, state)
        print(f"{pair}: пропускаємо аналіз входу — поруч важлива новина ({news_hit['title']})")
        return

    trend = get_trend_30m(pair)
    if trend is None:
        print(f"{pair}: недостатньо даних 30m")
        return

    df5 = fetch_candles(pair, interval="5m", period="5d")
    if len(df5) < IMPULSE_LOOKBACK_5M:
        print(f"{pair}: недостатньо даних 5m")
        return

    low, high, window = find_last_impulse(df5)
    ladder = build_entry_ladder(window, low, high, trend)
    if not ladder:
        print(f"{pair}: не вдалось побудувати рівні (мало даних)")
        return

    current_price = float(df5["Close"].iloc[-1])
    breakout = is_impulsive_breakout(df5, trend)

    for level in ladder:
        if not close_enough(current_price, level["price"]):
            continue

        # якщо виявлено імпульсний пробій — перекриття (рівні 2 і 3) не
        # пропонуються взагалі, лишається тільки базовий вхід з попередженням
        if breakout and level["level"] > 1:
            print(f"{pair}: рівень {level['level']} пропущено — імпульсний пробій")
            continue

        # анти-дубль: щоб не слати той самий рівень повторно, поки ціна від
        # нього не відійшла — прапорець тримається, доки close_enough не стане False
        signal_key = f"{pair}_{trend}_level{level['level']}"
        already_active = state.get(signal_key, False)
        if already_active:
            continue

        msg = format_signal(pair, trend, current_price, level, breakout)
        send_telegram_message(msg)
        state[signal_key] = True
        print(f"{pair}: сигнал відправлено (рівень {level['level']}, пробій={breakout})")
        return  # один сигнал на пару за прогін

    # ціна не біля жодного рівня зараз — знімаємо прапорці, щоб рівні могли
    # спрацювати знову при наступному підході
    for level in ladder:
        state[f"{pair}_{trend}_level{level['level']}"] = False

    print(f"{pair}: тренд={trend}, ціна={current_price:.5f}, рівні={[round(l['price'],5) for l in ladder]}")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------


def main():
    if not in_trading_window():
        print("Поза торговим вікном (10:00-20:00 Kyiv) — пропускаємо запуск.")
        return

    state = load_state()
    news_events = fetch_high_impact_events()

    for pair in PAIRS:
        try:
            check_pair(pair, state, news_events)
        except Exception as e:
            print(f"[Помилка] {pair}: {e}")
    save_state(state)


if __name__ == "__main__":
    main()"""
Forex Price-Action Signal Bot
------------------------------
Логіка:
  1. 30m графік: тренд визначається за EMA200 (ціна вище -> BUY, нижче -> SELL).
  2. 5m графік: знаходиться останній імпульс (swing high/low), рахуються рівні
     Фібоначчі відкату (38.2 / 50 / 61.8%).
  3. POC (Point of Control) рахується без реального обсягу (на форексі його
     немає) — ціновий діапазон імпульсу ділиться на 20 зон, і зоною POC
     вважається та, де ціна (за хвостами свічок) "затрималась" найдовше
     (найбільше touch count).
  4. Сигнал формується, якщо поточна ціна одночасно:
       - близько до одного з рівнів Фібо (в межах TOLERANCE_PCT)
       - близько до POC (в межах TOLERANCE_PCT)
     і напрямок збігається з трендом 30m.
  5. Надсилається HTML-повідомлення в Telegram.

Запуск: розрахований на одноразовий виклик за розкладом (GitHub Actions cron).
Ніякого `while True` — плануванням займається сам GitHub Actions.
Дедуп (щоб не спамити тим самим сигналом) робиться через файл last_signals.json.
"""

import json
import os
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
import requests

# ---------------------------------------------------------------------------
# НАЛАШТУВАННЯ
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PASTE_YOUR_CHAT_ID_HERE")

PAIRS = [
    "AUDCAD=X", "AUDCHF=X", "AUDUSD=X", "AUDJPY=X",
    "CADCHF=X", "CADJPY=X", "CHFJPY=X",
    "EURAUD=X", "EURCAD=X", "EURCHF=X", "EURGBP=X", "EURJPY=X", "EURUSD=X",
    "GBPAUD=X", "GBPCAD=X", "GBPCHF=X", "GBPJPY=X", "GBPUSD=X",
    "USDCAD=X", "USDCHF=X", "USDJPY=X",
]

# Робоче вікно (за твоїм часовим поясом Europe/Kyiv). Поза цим вікном скрипт
# завершується одразу, не роблячи запитів.
TZ = ZoneInfo("Europe/Kyiv")
WORK_START = dtime(10, 0)
WORK_END = dtime(20, 0)

TREND_EMA_PERIOD = 200          # EMA на 30m для визначення тренду
POC_ZONES = 30                  # на скільки цінових зон ділимо діапазон
TOLERANCE_PCT = 0.00015         # ~0.015% — наскільки близько ціна має підійти (звужено)
IMPULSE_LOOKBACK_5M = 150       # ширша історія 5m, щоб ловити "старі" рівні
ZONE_MERGE_PCT = 0.0015         # зони ближче ніж 0.15% одна до одної зливаються
MIN_ZONE_TOUCHES = 4            # зона вважається рівнем, тільки якщо ціна
                                 # торкалась її мінімум стільки разів — слабші
                                 # зони (шум) до драбини входів не потрапляють
BREAKOUT_RANGE_MULT = 1.8       # свічка вважається "імпульсним пробоєм", якщо
                                 # її діапазон (High-Low) у стільки разів більший
                                 # за середній діапазон останніх свічок
BREAKOUT_LOOKBACK = 20          # на скількох останніх свічках рахуємо середній діапазон
# Час експірації по рівнях драбини входів (підтверджено скріншотами реальних
# угод трейдера): базовий вхід — 5 хв, кожне наступне перекриття — 10 хв.
LEVEL_EXPIRY_MIN = {1: 5, 2: 10, 3: 10}
LEVEL_LABELS = {1: "Основа", 2: "1-е перекриття", 3: "2-е перекриття (POC)"}

# --- Економічний календар (Forex Factory, безкоштовно, без ключа) ---
NEWS_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_BEFORE_MIN = 15            # за скільки хвилин ДО новини попереджати
NEWS_AFTER_MIN = 20             # скільки хвилин ПІСЛЯ новини ще не торгувати
# Валюти, які цікавлять (витягуються з тікерів пар автоматично, це просто мапа
# ISO-коду з календаря на позначення в тікерах yfinance)
WATCHED_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF"}

STATE_FILE = os.path.join(os.path.dirname(__file__), "last_signals.json")

# ---------------------------------------------------------------------------
# ДОПОМІЖНІ ФУНКЦІЇ
# ---------------------------------------------------------------------------


def in_trading_window() -> bool:
    now = datetime.now(TZ).time()
    return WORK_START <= now <= WORK_END


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def fetch_candles(pair: str, interval: str, period: str) -> pd.DataFrame:
    df = yf.download(pair, interval=interval, period=period, progress=False, auto_adjust=False)
    if df is None or df.empty:
        return pd.DataFrame()
    # yfinance інколи повертає мультиіндекс колонок — приводимо до простого вигляду
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna()
    return df


def get_trend_30m(pair: str) -> str | None:
    df = fetch_candles(pair, interval="30m", period="30d")
    if len(df) < TREND_EMA_PERIOD:
        return None
    df["EMA"] = df["Close"].ewm(span=TREND_EMA_PERIOD, adjust=False).mean()
    last_close = df["Close"].iloc[-1]
    last_ema = df["EMA"].iloc[-1]
    return "BUY" if last_close > last_ema else "SELL"


def find_last_impulse(df: pd.DataFrame):
    """Дуже спрощений пошук останнього імпульсу: бере діапазон
    high/low за останні IMPULSE_LOOKBACK_5M свічок."""
    window = df.tail(IMPULSE_LOOKBACK_5M)
    impulse_high = window["High"].max()
    impulse_low = window["Low"].min()
    return impulse_low, impulse_high, window


def compute_zone_touches(window: pd.DataFrame, low: float, high: float) -> list[tuple[float, int]]:
    """Ділить діапазон [low, high] на POC_ZONES смуг і рахує, скільки разів
    ціна (High-Low кожної свічки) торкалась кожної смуги.
    Повертає список (ціна_центру_зони, кількість_дотиків)."""
    if high <= low:
        return [((high + low) / 2, 1)]
    zone_size = (high - low) / POC_ZONES
    touches = [0] * POC_ZONES

    for _, row in window.iterrows():
        zone_lo = int((row["Low"] - low) / zone_size)
        zone_hi = int((row["High"] - low) / zone_size)
        zone_lo = max(0, min(POC_ZONES - 1, zone_lo))
        zone_hi = max(0, min(POC_ZONES - 1, zone_hi))
        for z in range(zone_lo, zone_hi + 1):
            touches[z] += 1

    zones = [(low + zone_size * (i + 0.5), touches[i]) for i in range(POC_ZONES) if touches[i] > 0]
    return zones


def merge_close_zones(zones: list[tuple[float, int]]) -> list[tuple[float, int]]:
    """Зливає сусідні цінові зони, якщо вони ближче ніж ZONE_MERGE_PCT одна
    до одної (щоб не рахувати один і той самий рівень кілька разів)."""
    if not zones:
        return []
    zones_sorted = sorted(zones, key=lambda z: z[0])
    merged = [zones_sorted[0]]
    for price, touches in zones_sorted[1:]:
        last_price, last_touches = merged[-1]
        if abs(price - last_price) / last_price <= ZONE_MERGE_PCT:
            # зливаємо: середньозважена ціна, сума дотиків
            total = last_touches + touches
            new_price = (last_price * last_touches + price * touches) / total
            merged[-1] = (new_price, total)
        else:
            merged.append((price, touches))
    return merged


def build_entry_ladder(window: pd.DataFrame, low: float, high: float, trend: str) -> list[dict]:
    """Будує драбину з до 3 рівнів входу за мотивами стратегії трейдера:
      - Рівень 3 = POC (зона з максимальною кількістю дотиків ціни).
      - Рівні 1 і 2 = наступні за силою зони, розташовані МІЖ поточним краєм
        імпульсу і POC, впорядковані від найближчої до найдальшої (тобто
        рівень 1 спрацьовує першим на неглибокому відкаті, рівень 2 — на
        глибшому, рівень 3 (POC) — останній, найглибший відкат)."""
    raw_zones = compute_zone_touches(window, low, high)
    zones = merge_close_zones(raw_zones)
    # відсікаємо слабкі зони — лишаємо тільки ті, де ціна дійсно "затримувалась"
    zones = [z for z in zones if z[1] >= MIN_ZONE_TOUCHES]
    if not zones:
        return []

    zones_sorted_by_strength = sorted(zones, key=lambda z: z[1], reverse=True)
    poc_price, poc_touches = zones_sorted_by_strength[0]

    # Точка відліку відкату: верх імпульсу для BUY (ціна відкочується вниз
    # до рівнів), низ імпульсу для SELL (ціна відкочується вгору).
    edge = high if trend == "BUY" else low

    other_zones = [z for z in zones_sorted_by_strength[1:] if z[0] != poc_price]
    # серед інших зон беремо ті, що лежать між edge і POC (на шляху відкату)
    if trend == "BUY":
        candidates = [z for z in other_zones if poc_price <= z[0] <= edge]
        candidates.sort(key=lambda z: -z[0])  # від найближчої до edge до найдальшої
    else:
        candidates = [z for z in other_zones if edge <= z[0] <= poc_price]
        candidates.sort(key=lambda z: z[0])

    level_prices = [z[0] for z in candidates[:2]]  # рівень 1, рівень 2
    level_prices.append(poc_price)                 # рівень 3 = POC

    ladder = []
    for idx, price in enumerate(level_prices, start=1):
        ladder.append({
            "level": idx,
            "price": price,
            "expiry_min": LEVEL_EXPIRY_MIN.get(idx, 5),
            "is_poc": idx == 3,
        })
    return ladder


def close_enough(price: float, target: float) -> bool:
    return abs(price - target) / target <= TOLERANCE_PCT


def is_impulsive_breakout(df5: pd.DataFrame, trend: str) -> bool:
    """Перевіряє, чи остання свічка (і при потребі одна-дві перед нею)
    'пробиває рівень як ніж масло': діапазон значно більший за середній,
    і закриття йде далеко за рівень без відбою (без довгої протилежної тіні).
    Якщо так — перекриття (1-е/2-е) не пропонуються, тільки базовий вхід
    або взагалі нічого, з рекомендацією фіксованої ставки."""
    recent = df5.tail(BREAKOUT_LOOKBACK)
    if len(recent) < 5:
        return False

    avg_range = (recent["High"] - recent["Low"]).mean()
    last = df5.iloc[-1]
    last_range = last["High"] - last["Low"]
    if avg_range <= 0 or last_range < avg_range * BREAKOUT_RANGE_MULT:
        return False

    body = abs(last["Close"] - last["Open"])
    # тіло свічки має займати більшість діапазону (мала відбійна тінь)
    if last_range <= 0 or body / last_range < 0.6:
        return False

    # напрямок свічки має збігатись з трендом (імпульс по тренду, не проти нього)
    candle_up = last["Close"] > last["Open"]
    if trend == "BUY" and not candle_up:
        return False
    if trend == "SELL" and candle_up:
        return False

    return True


def send_telegram_message(html_text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": html_text,
        "parse_mode": "HTML",
    }
    resp = requests.post(url, data=payload, timeout=15)
    if resp.status_code != 200:
        print(f"[Telegram error] {resp.status_code}: {resp.text}")


def format_signal(pair: str, trend: str, price: float, level: dict, breakout: bool) -> str:
    clean_pair = pair.replace("=X", "")
    direction = "🟢 BUY (CALL)" if trend == "BUY" else "🔴 SELL (PUT)"
    level_label = LEVEL_LABELS.get(level["level"], f"Рівень {level['level']}")
    warning = ""
    if breakout:
        warning = (
            "\n⚠️ Схоже на імпульсний пробій рівня — "
            "рекомендована ФІКСОВАНА ставка, без перекриттів."
        )
    return (
        f"<b>Сигнал: {clean_pair}</b>\n"
        f"Напрямок: {direction}\n"
        f"{level_label}\n"
        f"Ціна входу: <code>{price:.5f}</code>\n"
        f"Ціна рівня: <code>{level['price']:.5f}</code>\n"
        f"Експірація: {level['expiry_min']} хв"
        f"{warning}\n"
        f"Час: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')} (Kyiv)"
    )


# ---------------------------------------------------------------------------
# ЕКОНОМІЧНИЙ КАЛЕНДАР
# ---------------------------------------------------------------------------


def fetch_high_impact_events() -> list[dict]:
    """Тягне календар на поточний тиждень і лишає тільки High impact події."""
    try:
        resp = requests.get(NEWS_CALENDAR_URL, timeout=15)
        resp.raise_for_status()
        events = resp.json()
    except Exception as e:
        print(f"[Календар] не вдалось отримати дані: {e}")
        return []

    high_impact = []
    for ev in events:
        if ev.get("impact") != "High":
            continue
        if ev.get("country") not in WATCHED_CURRENCIES:
            continue
        try:
            ev_time = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(TZ)
        except Exception:
            continue
        high_impact.append({
            "title": ev.get("title", "Подія"),
            "currency": ev.get("country"),
            "time": ev_time,
        })
    return high_impact


def find_relevant_news_window(pair: str, events: list[dict]):
    """Повертає подію, якщо зараз потрапляємо у вікно 'до/після новини' для
    валют, що входять у дану пару."""
    clean = pair.replace("=X", "")
    pair_currencies = {clean[:3], clean[3:6]}
    now = datetime.now(TZ)

    for ev in events:
        if ev["currency"] not in pair_currencies:
            continue
        delta_min = (ev["time"] - now).total_seconds() / 60
        if -NEWS_AFTER_MIN <= delta_min <= NEWS_BEFORE_MIN:
            return ev
    return None


def maybe_send_news_warning(pair: str, event: dict, state: dict) -> None:
    clean_pair = pair.replace("=X", "")
    key = f"news_{clean_pair}_{event['title']}_{event['time'].strftime('%Y%m%d%H%M')}"
    if state.get(key):
        return  # вже попереджали про цю подію для цієї пари

    msg = (
        f"⚠️ <b>Скоро важлива новина: {clean_pair}</b>\n"
        f"Подія: {event['title']} ({event['currency']})\n"
        f"Час виходу: {event['time'].strftime('%H:%M')} (Kyiv)\n"
        f"Рекомендація: не торгувати ~{NEWS_BEFORE_MIN} хв до і "
        f"~{NEWS_AFTER_MIN} хв після виходу."
    )
    send_telegram_message(msg)
    state[key] = True
    print(f"{pair}: попередження про новину відправлено")


# ---------------------------------------------------------------------------
# ОСНОВНА ЛОГІКА ПО ОДНІЙ ПАРІ
# ---------------------------------------------------------------------------


def check_pair(pair: str, state: dict, news_events: list[dict]) -> None:
    news_hit = find_relevant_news_window(pair, news_events)
    if news_hit:
        maybe_send_news_warning(pair, news_hit, state)
        print(f"{pair}: пропускаємо аналіз входу — поруч важлива новина ({news_hit['title']})")
        return

    trend = get_trend_30m(pair)
    if trend is None:
        print(f"{pair}: недостатньо даних 30m")
        return

    df5 = fetch_candles(pair, interval="5m", period="5d")
    if len(df5) < IMPULSE_LOOKBACK_5M:
        print(f"{pair}: недостатньо даних 5m")
        return

    low, high, window = find_last_impulse(df5)
    ladder = build_entry_ladder(window, low, high, trend)
    if not ladder:
        print(f"{pair}: не вдалось побудувати рівні (мало даних)")
        return

    current_price = float(df5["Close"].iloc[-1])
    breakout = is_impulsive_breakout(df5, trend)

    for level in ladder:
        if not close_enough(current_price, level["price"]):
            continue

        # якщо виявлено імпульсний пробій — перекриття (рівні 2 і 3) не
        # пропонуються взагалі, лишається тільки базовий вхід з попередженням
        if breakout and level["level"] > 1:
            print(f"{pair}: рівень {level['level']} пропущено — імпульсний пробій")
            continue

        # анти-дубль: щоб не слати той самий рівень повторно, поки ціна від
        # нього не відійшла — прапорець тримається, доки close_enough не стане False
        signal_key = f"{pair}_{trend}_level{level['level']}"
        already_active = state.get(signal_key, False)
        if already_active:
            continue

        msg = format_signal(pair, trend, current_price, level, breakout)
        send_telegram_message(msg)
        state[signal_key] = True
        print(f"{pair}: сигнал відправлено (рівень {level['level']}, пробій={breakout})")
        return  # один сигнал на пару за прогін

    # ціна не біля жодного рівня зараз — знімаємо прапорці, щоб рівні могли
    # спрацювати знову при наступному підході
    for level in ladder:
        state[f"{pair}_{trend}_level{level['level']}"] = False

    print(f"{pair}: тренд={trend}, ціна={current_price:.5f}, рівні={[round(l['price'],5) for l in ladder]}")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------


def main():
    if not in_trading_window():
        print("Поза торговим вікном (10:00-20:00 Kyiv) — пропускаємо запуск.")
        return

    state = load_state()
    news_events = fetch_high_impact_events()

    for pair in PAIRS:
        try:
            check_pair(pair, state, news_events)
        except Exception as e:
            print(f"[Помилка] {pair}: {e}")
    save_state(state)


if __name__ == "__main__":
    main()
