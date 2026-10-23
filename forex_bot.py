"""
Forex Price-Action Signal Bot — v30.3
FIX 22: POC-інверсія
FIX 23: soft-log
FIX 24: трекінг траєкторії
FIX 25: health-check + weekly tech report
FIX 26: ATR-залежна ширина зони
FIX 27: мінімум 3 зони в ladder
FIX 28: примусове розбиття широких зон
FIX 29: фіксація драбини в state
FIX 30: швидкий кулдаун (3 хв) та логіка мартингейлу
FIX 31: анти-спам — стабільний ключ драбини, захист рівнів (2 год),
        пауза 35 хв після будь-якого WIN, ліміт L1 за годину
FIX 32: інформативний формат Telegram (клас сигналу, сила зони,
        тренд M30 + локальний тренд M5, блок "що це значить")
FIX 33: журнал пише тільки реальні відправлення
FIX 34: рядок "Відкат M5" показує стан відкату, а не "проти сигналу"
FIX 35: замок серії — поки по парі є невирішений результат,
        жодних нових сигналів по цій парі
FIX 36: крокова машина серії — сигнал тільки від зони поточного кроку
        (1 -> 2 -> 3, ніколи навпаки); крок росте після LOSS або
        після пробою зони ціною; WIN закриває серію
FIX 37: повернені Telegram-попередження про важливі новини
FIX 38: виправлена build_volume_zones — прибрано нескінченний цикл
FIX 39: heartbeat-рядок у логах кожного циклу
FIX 40: кінцевий автомат серій (State Machine) — чітка логіка
        L1 -> L2 -> L3 з таймерами та заморозкою
FIX 41: Л2 тільки реальний (зона між Л1 і Л3), штучні середини відкидаються
FIX 42: фільтр MIN_POC_STRENGTH — драбина відкидається якщо POC < 10%
FIX 43: ДЕННИЙ POC — беремо тільки свічки M5 з початку сьогоднішнього дня
FIX 44: Фільтр рівномірності драбини — Л2 на 30-70% шляху, мін. 10 піпсів
FIX 45: Візуальні полиці — Л2 має мати дотики з обох сторін (above/below >= 2)
FIX 46: Адаптивна ширина зони — мін. 30% ATR, консолідація 3+ свічки
FIX 47: Виправлено score = 0 (тепер рахується правильно)
FIX 48: НОВИЙ ДИЗАЙН повідомлень — рамка з кольорових квадратів, прогрес-бари, дашборд
FIX 49: білий список Medium-новин (claims, crude oil, monetary policy тощо)
FIX 50: алерт у Telegram, якщо календар новин недоступний >6 год
FIX 51: ранковий огляд новин на день (~10:00) одним повідомленням
FIX 52: одне попередження на подію, а не на кожну пару
FIX 55: ПОСТІЙНИЙ ЛОГ СКАНУВАННЯ — кожен цикл друкує причину по кожній парі
FIX 56: новинні попередження по всіх подіях вікна за один прохід +
        адаптивний текст ("щойно вийшла" замість "скоро")
"""
import csv
import json
import os
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PASTE_YOUR_CHAT_ID_HERE")

PAIRS = [
    "AUDCAD=X", "AUDCHF=X", "AUDUSD=X", "AUDJPY=X",
    "CADCHF=X", "CADJPY=X", "CHFJPY=X",
    "EURAUD=X", "EURCAD=X", "EURCHF=X", "EURGBP=X", "EURJPY=X", "EURUSD=X",
    "GBPAUD=X", "GBPCAD=X", "GBPCHF=X", "GBPJPY=X", "GBPUSD=X",
    "USDCAD=X", "USDCHF=X", "USDJPY=X",
]

TZ = ZoneInfo("Europe/Kyiv")
WORK_START = dtime(10, 0)
WORK_END = dtime(20, 0)

RUN_MODE = os.environ.get("RUN_MODE", "once")
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "60"))
BETWEEN_PAIRS_SLEEP_SEC = 1.0
OUT_OF_WINDOW_SLEEP_SEC = 300
MAX_SIGNALS_PER_RUN = 2

TREND_EMA_PERIOD = 200
STRUCTURE_LOOKBACK = 24
CONSOLIDATION_LOOKBACK = 48
REQUIRE_GLOBAL_AGREEMENT = True
GLOBAL_DIST_ATR = 1.0
EFFICIENCY_MIN = 0.15
NET_MOVE_ATR_MIN = 2.0

IMPULSE_CONFIRM_LOOKBACK = 8
IMPULSE_CONFIRM_REF = 24

POC_ZONES = 30
TOLERANCE_PCT = 0.00015
IMPULSE_LOOKBACK_5M = 150
PROFILE_LOOKBACK_5M = 200
VALLEY_FRACTION = 0.15
MAX_ZONE_WIDTH_ATR = 1.5
MIN_ZONE_TOUCHES = 4
MIN_TWO_SIDED = 1
MIN_WICK_RATIO = 0.35

MIN_PULLBACK_FRACTION = 0.10
MIN_SCORE = 30
MIN_LADDER_ZONES = 3

DWELL_LOOKBACK = 12
DWELL_MAX_CLOSES = 6

SIGNAL_COOLDOWN_MIN = 3
BREAKOUT_RANGE_MULT = 1.8
BREAKOUT_LOOKBACK = 20

WIN_PAUSE_MIN = 35
LEVEL_REPEAT_MIN = 120
MAX_L1_PER_HOUR = 4

LEVEL_EXPIRY_BASE_MIN = 5
LEVEL_EXPIRY_MID_MIN = 15
LEVEL_EXPIRY_POC_MIN = 15

ABANDON_MIN = 30

L2_TIMEOUT_MIN = 15
L3_WAIT_MIN = 35
FREEZE_MIN = 40

MIN_POC_STRENGTH = 0.10

MIN_DAILY_CANDLES = 60
MIN_LEVEL_DISTANCE_PIPS = 10
L2_POSITION_MIN = 0.30
L2_POSITION_MAX = 0.70
MIN_TWO_SIDED_TOUCHES = 2
MIN_ZONE_WIDTH_ATR = 0.30
MIN_CANDLES_IN_ZONE = 3

# FIX 48: Константи для нового дизайну
DIV = "┈" * 22
MAX_SCORE = 150
DIRECTION = {
    "BUY": ("🟢", "▲", "BUY (CALL)"),
    "SELL": ("🔴", "▼", "SELL (PUT)")
}
STRENGTH = {
    "vweak": ("🔴", "ДУЖЕ СЛАБКИЙ"),
    "weak": ("🟠", "СЛАБКИЙ"),
    "medium": ("🟡", "ПОМІРНИЙ"),
    "strong": ("🟢", "СИЛЬНИЙ")
}
ICON = {k: v[0] for k, v in STRENGTH.items()}
SCALE = {"zone": 20.0, "trend": 1.0, "pullback": 1.0}

NEWS_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_CACHE_TTL_HOURS = 4
NEWS_BEFORE_MIN = 25
NEWS_AFTER_MIN = 20
WATCHED_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF"}

MEDIUM_BLOCK_KEYWORDS = [
    "claims", "crude oil", "natural gas", "monetary policy", "interest rate",
    "cpi", "ppi", "gdp", "retail", "pmi", "nonfarm", "fomc",
    "trade balance", "consumer price", "unemployment",
]

NEWS_MAX_AGE_HOURS = 6

# FIX 55: буфер причин пропуску за поточний цикл
SCAN_LOG = []

TRAJECTORY_POINTS = [2, 4, 6, 8, 10, 12, 14]
TRACKING_HEALTH_HOURS = 24
TRACKING_EMERGENCY_HOURS = 48

DAILY_REPORT_HOUR = 19
DAILY_REPORT_MINUTE = 55
WEEKLY_REPORT_DAY = 6
WEEKLY_REPORT_HOUR = 20

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "last_signals.json")
JOURNAL_FILE = os.path.join(BASE_DIR, "signals_journal.csv")
OUTCOMES_FILE = os.path.join(BASE_DIR, "outcomes_journal.csv")
TRAJECTORY_FILE = os.path.join(BASE_DIR, "trajectory_journal.csv")
NEWS_CACHE_FILE = os.path.join(BASE_DIR, "ff_calendar_cache.json")

JOURNAL_FIELDS = ["ts", "pair", "direction", "level", "zone_lo", "zone_hi",
                  "entry_price", "expiry_min", "score", "strength_pct", "er",
                  "trend_source", "sent", "reason",
                  "atr_value", "atr_ratio", "strength_bucket",
                  "hour_utc", "day_of_week", "is_cpi_day"]
OUTCOME_FIELDS = ["ts", "pair", "direction", "entry_price", "exit_price", "result",
                  "strength_pct", "atr_ratio"]
TRAJECTORY_FIELDS = ["signal_ts", "pair", "direction", "entry_price",
                     "strength_pct", "atr_ratio", "strength_bucket",
                     "hour_utc", "day_of_week", "is_cpi_day",
                     "p2m", "p4m", "p6m", "p8m", "p10m", "p12m", "p14m",
                     "res2m", "res4m", "res6m", "res8m", "res10m", "res12m", "res14m"]


def in_trading_window() -> bool:
    now_dt = datetime.now(TZ)
    if now_dt.weekday() >= 5:
        return False
    return WORK_START <= now_dt.time() <= WORK_END


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def fetch_candles(pair: str, interval: str, period: str) -> pd.DataFrame:
    df = yf.download(pair, interval=interval, period=period,
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna()


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period:
        return 0.0
    high = df["High"].tail(period)
    low = df["Low"].tail(period)
    close = df["Close"].tail(period)
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    return float(tr.mean())


def compute_atr_ratio(df: pd.DataFrame, period: int = 14, avg_period: int = 20) -> float:
    if len(df) < period + avg_period:
        return 1.0
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    atr_series = tr.rolling(period).mean()
    current_atr = float(atr_series.iloc[-1])
    avg_atr = float(atr_series.tail(avg_period).mean())
    if avg_atr <= 0:
        return 1.0
    return current_atr / avg_atr


def classify_strength(strength: float) -> str:
    if strength < 0.05:
        return "weak"
    elif strength < 0.20:
        return "medium"
    else:
        return "strong"


def is_cpi_day_today(news_events) -> bool:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    for ev in news_events:
        if ev.get("currency") != "USD" or "CPI" not in ev.get("title", ""):
            continue
        ev_time = ev.get("time")
        if ev_time and ev_time.strftime("%Y-%m-%d") == today:
            return True
    return False


def trend_by_ema(df: pd.DataFrame, period: int):
    if len(df) < period:
        return None
    ema = df["Close"].ewm(span=period, adjust=False).mean()
    return "BUY" if df["Close"].iloc[-1] > ema.iloc[-1] else "SELL"


def trend_by_structure(df: pd.DataFrame, lookback: int):
    if len(df) < lookback:
        return None
    window = df.tail(lookback)
    half = lookback // 2
    first_half, second_half = window.iloc[:half], window.iloc[half:]
    first_high, first_low = first_half["High"].max(), first_half["Low"].min()
    second_high, second_low = second_half["High"].max(), second_half["Low"].min()
    if second_high > first_high and second_low > first_low:
        return "BUY"
    if second_high < first_high and second_low < first_low:
        return "SELL"
    return None


def efficiency_ratio(closes: pd.Series, lookback: int) -> float:
    if len(closes) < lookback + 1:
        return 0.0
    w = closes.tail(lookback + 1)
    volatility = w.diff().abs().sum()
    return abs(w.iloc[-1] - w.iloc[0]) / volatility if volatility > 0 else 0.0


def recent_impulse_confirms(df: pd.DataFrame, trend: str) -> tuple:
    if len(df) < IMPULSE_CONFIRM_REF:
        return False, "мало даних"
    window = df.tail(IMPULSE_CONFIRM_REF)
    recent = df.tail(IMPULSE_CONFIRM_LOOKBACK)
    net = recent["Close"].iloc[-1] - recent["Close"].iloc[0]
    if trend == "BUY":
        if net <= 0:
            return False, "нетто-рух вниз"
        if recent["High"].max() < window["High"].max() - 1e-6:
            return False, "не оновили максимум"
        return True, "ok"
    else:
        if net >= 0:
            return False, "нетто-рух вгору"
        if recent["Low"].min() > window["Low"].min() + 1e-6:
            return False, "не оновили мінімум"
        return True, "ok"


def analyze_trend_30m(df: pd.DataFrame):
    if len(df) < CONSOLIDATION_LOOKBACK + 1:
        return None, "мало даних 30m", 0.0
    atr = compute_atr(df, 14)
    if atr <= 0:
        return None, "atr=0", 0.0
    er = efficiency_ratio(df["Close"], CONSOLIDATION_LOOKBACK)
    w48 = df.tail(CONSOLIDATION_LOOKBACK)
    net48 = w48["Close"].iloc[-1] - w48["Close"].iloc[0]
    if er < EFFICIENCY_MIN or abs(net48) < NET_MOVE_ATR_MIN * atr:
        return None, f"консолідація (er={er:.2f})", er

    trend, source = None, None
    w24 = df.tail(STRUCTURE_LOOKBACK)
    net24 = w24["Close"].iloc[-1] - w24["Close"].iloc[0]
    s24 = trend_by_structure(df, STRUCTURE_LOOKBACK)
    if s24 is not None and s24 == ("BUY" if net24 > 0 else "SELL"):
        trend, source = s24, "structure24"
    if trend is None:
        s48 = trend_by_structure(df, CONSOLIDATION_LOOKBACK)
        if s48 is not None and s48 == ("BUY" if net48 > 0 else "SELL"):
            trend, source = s48, "structure48"

    ema_series = df["Close"].ewm(span=TREND_EMA_PERIOD, adjust=False).mean()
    ema_dir = "BUY" if df["Close"].iloc[-1] > ema_series.iloc[-1] else "SELL"
    dist = abs(df["Close"].iloc[-1] - ema_series.iloc[-1])
    global_readable = len(df) >= TREND_EMA_PERIOD and dist >= GLOBAL_DIST_ATR * atr

    if trend is None and global_readable:
        trend, source = ema_dir, "ema200"
    if trend is None:
        return None, "немає чіткого тренда", er
    if REQUIRE_GLOBAL_AGREEMENT and global_readable and trend != ema_dir:
        return None, f"неузгодженість: {source} vs EMA200", er

    ok, why = recent_impulse_confirms(df, trend)
    if not ok:
        return None, f"тренд {trend} не підтверджений: {why}", er
    return trend, source, er


def compute_volume_profile(window: pd.DataFrame, low: float, high: float):
    if high <= low:
        mid = (high + low) / 2
        return [{"lo": mid, "hi": mid, "vol": 1.0, "touches": 1, "above": 1, "below": 1}]
    zone_size = (high - low) / POC_ZONES
    touches = [0] * POC_ZONES
    vol = [0.0] * POC_ZONES
    above = [0] * POC_ZONES
    below = [0] * POC_ZONES
    has_vol = "Volume" in window.columns and window["Volume"].sum() > 0
    for _, row in window.iterrows():
        z_lo = max(0, min(POC_ZONES - 1, int((row["Low"] - low) / zone_size)))
        z_hi = max(0, min(POC_ZONES - 1, int((row["High"] - low) / zone_size)))
        span = z_hi - z_lo + 1
        v = (row["Volume"] / span) if has_vol else (1.0 / span)
        for z in range(z_lo, z_hi + 1):
            touches[z] += 1
            vol[z] += v
        above[z_lo] += 1
        below[z_hi] += 1
    cells = []
    for i in range(POC_ZONES):
        cells.append({"lo": low + zone_size * i, "hi": low + zone_size * (i + 1),
                      "vol": vol[i], "touches": touches[i], "above": above[i], "below": below[i]})
    return cells


def split_wide_segment(idx, vols, cells, cap_width):
    lo_i, hi_i = idx[0], idx[-1]
    width = cells[hi_i]["hi"] - cells[lo_i]["lo"]
    if width <= cap_width:
        return [idx]
    if len(idx) <= 2:
        mid = len(idx) // 2
        return [idx[:mid], idx[mid:]]
    interior = idx[1:-1]
    m = min(interior, key=lambda k: vols[k])
    left = [k for k in idx if k <= m]
    right = [k for k in idx if k > m]
    return (split_wide_segment(left, vols, cells, cap_width) + split_wide_segment(right, vols, cells, cap_width))


def build_volume_zones(cells, max_width):
    raw = [c["vol"] for c in cells]
    total = sum(raw)
    if total <= 0:
        return []
    n = len(cells)
    vols = []
    for i in range(n):
        vols.append(sum(raw[max(0, i-1):min(n, i+2)]) / len(raw[max(0, i-1):min(n, i+2)]))
    max_vol = max(vols)
    if max_vol <= 0:
        return []
    is_valley = [vols[i] < VALLEY_FRACTION * max_vol for i in range(n)]
    zones = []
    i = 0
    while i < n:
        if is_valley[i] or raw[i] <= 0:
            i += 1
            continue
        j = i
        while j + 1 < n and raw[j + 1] > 0 and not is_valley[j + 1]:
            j += 1
        seg = list(range(i, j + 1))
        width = cells[j]["hi"] - cells[i]["lo"]
        if width > max_width:
            pieces = split_wide_segment(seg, vols, cells, max_width)
        else:
            pieces = [seg]
        for piece in pieces:
            lo_i, hi_i = piece[0], piece[-1]
            zone_vol = sum(raw[k] for k in piece)
            zones.append({"lo": cells[lo_i]["lo"], "hi": cells[hi_i]["hi"],
                          "touches": sum(cells[k]["touches"] for k in piece),
                          "above": sum(cells[k]["above"] for k in piece),
                          "below": sum(cells[k]["below"] for k in piece),
                          "share": zone_vol / total})
        i = j + 1
    return zones


def _mk_level(idx: int, z: dict, is_poc: bool) -> dict:
    if is_poc:
        expiry = LEVEL_EXPIRY_POC_MIN
    elif idx == 1:
        expiry = LEVEL_EXPIRY_BASE_MIN
    else:
        expiry = LEVEL_EXPIRY_MID_MIN

    return {"level": idx, "lo": z["lo"], "hi": z["hi"],
            "center": (z["lo"] + z["hi"]) / 2, "strength": z["share"],
            "above": z["above"], "below": z["below"], "is_poc": is_poc,
            "expiry_min": expiry}


def get_daily_profile_window(df5: pd.DataFrame) -> pd.DataFrame:
    if df5.empty:
        return df5
    today_start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    mask = df5.index >= today_start
    window = df5[mask]
    if len(window) < MIN_DAILY_CANDLES:
        window = df5.tail(MIN_DAILY_CANDLES)
    return window


def is_zone_valid(zone: dict, df5: pd.DataFrame, pair: str, atr: float) -> bool:
    pips_multiplier = 100 if "JPY" in pair else 10000
    width_pips = (zone["hi"] - zone["lo"]) * pips_multiplier
    min_width_pips = atr * pips_multiplier * MIN_ZONE_WIDTH_ATR
    if width_pips < min_width_pips:
        return False
    candles_in_zone = df5[
        (df5["Low"] <= zone["hi"]) & (df5["High"] >= zone["lo"])
    ]
    if len(candles_in_zone) < MIN_CANDLES_IN_ZONE:
        return False
    return True


def is_ladder_balanced(l1: dict, l2: dict, l3: dict, pair: str) -> bool:
    l1_c = (l1["lo"] + l1["hi"]) / 2
    l2_c = (l2["lo"] + l2["hi"]) / 2
    l3_c = (l3["lo"] + l3["hi"]) / 2
    total_distance = abs(l3_c - l1_c)
    if total_distance == 0:
        return False
    l2_position = abs(l2_c - l1_c) / total_distance
    if not (L2_POSITION_MIN <= l2_position <= L2_POSITION_MAX):
        return False
    pips_multiplier = 100 if "JPY" in pair else 10000
    l1_l2_pips = abs(l2_c - l1_c) * pips_multiplier
    l2_l3_pips = abs(l3_c - l2_c) * pips_multiplier
    if l1_l2_pips < MIN_LEVEL_DISTANCE_PIPS or l2_l3_pips < MIN_LEVEL_DISTANCE_PIPS:
        return False
    if l2.get("above", 0) < MIN_TWO_SIDED_TOUCHES or l2.get("below", 0) < MIN_TWO_SIDED_TOUCHES:
        return False
    return True


def build_entry_ladder(window_profile: pd.DataFrame, trend: str, pair: str):
    profile_low = window_profile["Low"].min()
    profile_high = window_profile["High"].max()
    atr = compute_atr(window_profile, 14)
    if atr <= 0:
        return []
    max_width = MAX_ZONE_WIDTH_ATR * atr
    cells = compute_volume_profile(window_profile, profile_low, profile_high)
    zones = [z for z in build_volume_zones(cells, max_width)
             if z["touches"] >= MIN_ZONE_TOUCHES
             and is_zone_valid(z, window_profile, pair, atr)]
    if not zones:
        return []
    poc = max(zones, key=lambda z: z["share"])
    poc_c = (poc["lo"] + poc["hi"]) / 2
    others = [z for z in zones if z is not poc]
    current_price = float(window_profile["Close"].iloc[-1])
    if trend == "BUY":
        if poc_c >= current_price:
            return []
        path = [z for z in others if poc_c <= (z["lo"] + z["hi"]) / 2 <= current_price]
        path.sort(key=lambda z: -(z["lo"] + z["hi"]) / 2)
    else:
        if poc_c <= current_price:
            return []
        path = [z for z in others if current_price <= (z["lo"] + z["hi"]) / 2 <= poc_c]
        path.sort(key=lambda z: (z["lo"] + z["hi"]) / 2)
    if not path:
        return []
    l1 = path[0]
    l1_c = (l1["lo"] + l1["hi"]) / 2
    l2_candidates = []
    for z in path[1:]:
        z_c = (z["lo"] + z["hi"]) / 2
        if trend == "BUY":
            if l1_c > z_c > poc_c:
                l2_candidates.append(z)
        else:
            if l1_c < z_c < poc_c:
                l2_candidates.append(z)
    if not l2_candidates:
        return []
    l2 = max(l2_candidates, key=lambda z: z["share"])
    if poc["share"] < MIN_POC_STRENGTH:
        return []
    ladder = [
        _mk_level(1, l1, False),
        _mk_level(2, l2, False),
        _mk_level(3, poc, True)
    ]
    if not is_ladder_balanced(ladder[0], ladder[1], ladder[2], pair):
        return []
    return ladder


def find_last_impulse(df: pd.DataFrame):
    window = df.tail(IMPULSE_LOOKBACK_5M)
    return window["Low"].min(), window["High"].max(), window


def in_zone(price: float, zone: dict, trend: str) -> bool:
    if trend == "BUY":
        return zone["lo"] * (1 - TOLERANCE_PCT) <= price <= zone["hi"]
    else:
        return zone["lo"] <= price <= zone["hi"] * (1 + TOLERANCE_PCT)


def approach_ok(df5: pd.DataFrame, zone: dict, trend: str) -> bool:
    if len(df5) < 4:
        return False
    recent = df5.iloc[-4:-1]
    if trend == "BUY":
        return recent["High"].max() >= zone["hi"]
    return recent["Low"].min() <= zone["lo"]


def zone_is_dwelling(df5: pd.DataFrame, zone: dict) -> bool:
    if len(df5) < DWELL_LOOKBACK + 1:
        return False
    closes = df5["Close"].iloc[-(DWELL_LOOKBACK + 1):-1]
    inside = ((closes >= zone["lo"]) & (closes <= zone["hi"])).sum()
    return inside >= DWELL_MAX_CLOSES


def zone_broken(zone: dict, df5: pd.DataFrame, trend: str) -> bool:
    if len(df5) < 2:
        return False
    c = df5.iloc[-2]
    if trend == "BUY":
        return c["Close"] < zone["lo"] * (1 - TOLERANCE_PCT)
    return c["Close"] > zone["hi"] * (1 + TOLERANCE_PCT)


def is_impulsive_breakout(df5: pd.DataFrame, trend: str) -> bool:
    if len(df5) < BREAKOUT_LOOKBACK + 2:
        return False
    recent = df5.iloc[-(BREAKOUT_LOOKBACK + 1):-1]
    last = df5.iloc[-2]
    avg_range = (recent["High"] - recent["Low"]).mean()
    last_range = last["High"] - last["Low"]
    if avg_range <= 0 or last_range < avg_range * BREAKOUT_RANGE_MULT:
        return False
    body = abs(last["Close"] - last["Open"])
    if last_range <= 0 or body / last_range < 0.6:
        return False
    candle_up = last["Close"] > last["Open"]
    if trend == "BUY" and not candle_up:
        return False
    if trend == "SELL" and candle_up:
        return False
    return True


def cooldown_active(state: dict, pair: str) -> bool:
    last_str = state.get(f"{pair}_last_signal_at")
    if not last_str:
        return False
    try:
        last_dt = datetime.strptime(last_str, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except Exception:
        return False
    return (datetime.now(TZ) - last_dt).total_seconds() / 60 < SIGNAL_COOLDOWN_MIN


def mark_signal_sent(state: dict, pair: str) -> None:
    state[f"{pair}_last_signal_at"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")


def l1_recent_count(state: dict) -> int:
    now = datetime.now(TZ)
    cnt = 0
    for k, v in state.items():
        if k.endswith("_level1_sent_at") and isinstance(v, str):
            try:
                dt = datetime.strptime(v, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
                if (now - dt).total_seconds() < 3600:
                    cnt += 1
            except Exception:
                pass
    return cnt


def send_telegram_message(html_text: str) -> bool:
    if not in_trading_window():
        print("[Telegram] пропущено — поза торговим вікном")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": html_text,
                                        "parse_mode": "HTML"}, timeout=15)
    except Exception as e:
        print(f"[Telegram error] {e}")
        return False
    if resp.status_code != 200:
        print(f"[Telegram error] {resp.status_code}: {resp.text}")
        return False
    return True


# ============================================================
# НОВИНИ: FIX 37 + FIX 49 + FIX 50 + FIX 51 + FIX 52 + FIX 56
# ============================================================
def _news_event_ok(e: dict) -> bool:
    imp = e.get("impact")
    if imp == "High":
        return True
    if imp == "Medium":
        title = e.get("title", "").lower()
        return any(k in title for k in MEDIUM_BLOCK_KEYWORDS)
    return False


def _load_news_cache():
    if not os.path.exists(NEWS_CACHE_FILE): return None
    try:
        with open(NEWS_CACHE_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return None


def fetch_high_impact_events():
    now = datetime.now(TZ)
    cache = _load_news_cache()
    if cache and cache.get("fetched_at"):
        try:
            age = (now - datetime.fromisoformat(cache["fetched_at"])).total_seconds() / 3600
            if age < NEWS_CACHE_TTL_HOURS:
                return [{"title": e["title"], "currency": e["country"],
                         "time": datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(TZ)}
                        for e in cache["data"]
                        if _news_event_ok(e) and e.get("country") in WATCHED_CURRENCIES]
        except Exception: pass

    try:
        resp = requests.get(NEWS_CALENDAR_URL, timeout=15)
        if resp.status_code == 200:
            raw = resp.json()
            with open(NEWS_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": now.isoformat(), "data": raw}, f)
            return [{"title": e["title"], "currency": e["country"],
                     "time": datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(TZ)}
                    for e in raw
                    if _news_event_ok(e) and e.get("country") in WATCHED_CURRENCIES]
    except Exception: pass
    return []


def news_health_alert(state: dict) -> None:
    if not in_trading_window():
        return
    age_h = 999.0
    cache = _load_news_cache()
    if cache and cache.get("fetched_at"):
        try:
            age_h = (datetime.now(TZ) - datetime.fromisoformat(cache["fetched_at"])).total_seconds() / 3600
        except Exception:
            age_h = 999.0
    if age_h > NEWS_MAX_AGE_HOURS and (time.time() - state.get("last_news_alert", 0)) > 86400:
        if send_telegram_message(
            f"⚠️ <b>Календар новин недоступний</b> (останнє оновлення: {age_h:.0f} год тому).\n"
            f"Фільтр новин ВИМКНЕНИЙ — перед входом перевіряй календар брокера вручну!"
        ):
            state["last_news_alert"] = time.time()


def send_daily_news_calendar(state: dict) -> None:
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    if state.get("last_news_calendar") == today:
        return
    events = fetch_high_impact_events()
    todays = [e for e in events
              if e["time"].strftime("%Y-%m-%d") == today
              and e["time"] >= now - timedelta(minutes=NEWS_AFTER_MIN)]
    todays.sort(key=lambda e: e["time"])
    lines = [f"📅 <b>Календар новин на {today} (дані бота)</b>"]
    if todays:
        for e in todays[:14]:
            lines.append(f"• {e['time'].strftime('%H:%M')} {e['currency']} — {e['title']}")
        if len(todays) > 14:
            lines.append(f"• …і ще {len(todays) - 14}")
    else:
        lines.append("• важливих новин у списку немає (High + білий список Medium)")
    lines.append(f"<i>Бот блокує відповідні пари за {NEWS_BEFORE_MIN} хв до і {NEWS_AFTER_MIN} хв після кожної події.</i>")
    if send_telegram_message("\n".join(lines)):
        state["last_news_calendar"] = today


def maybe_send_news_warning(pair: str, event: dict, state: dict) -> None:
    """FIX 52 + FIX 56: одне попередження на подію, адаптивний текст."""
    key = f"news_{event['title']}_{event['time'].strftime('%Y%m%d%H%M')}"
    if state.get(key):
        return
    delta = (event["time"] - datetime.now(TZ)).total_seconds() / 60
    if delta >= 0:
        when = f"Скоро вихід: {event['time'].strftime('%H:%M')} (Kyiv)"
    else:
        when = (f"Новина ВИЙШЛА о {event['time'].strftime('%H:%M')} (Kyiv), "
                f"вікно блокування ще діє")
    msg = (f"⚠️ <b>Важлива новина: {event['title']} ({event['currency']})</b>\n"
           f"{when}\n"
           f"Бот блокує всі пари з {event['currency']} за {NEWS_BEFORE_MIN} хв до "
           f"і {NEWS_AFTER_MIN} хв після.\n"
           f"Рекомендація: не торгуй ці валюти вручну у цьому вікні.")
    if send_telegram_message(msg):
        state[key] = True


def start_trajectory(state: dict, cand: dict, meta: dict) -> None:
    now_ts = time.time()
    checks = [{"min": m, "due_ts": now_ts + m * 60, "price": None} for m in TRAJECTORY_POINTS]
    state.setdefault("pending_trajectory", []).append({
        "pair": cand["pair"], "direction": cand["trend"], "entry": cand["entry"],
        "signal_ts": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        "strength_pct": float(round(float(cand["level"]["strength"]) * 100, 2)),
        "atr_ratio": meta.get("atr_ratio", 1.0), "strength_bucket": classify_strength(float(cand["level"]["strength"])),
        "hour_utc": meta.get("hour_utc", 0), "day_of_week": meta.get("day_of_week", 0),
        "is_cpi_day": meta.get("is_cpi_day", False), "checks": checks,
    })


def process_trajectory(state: dict) -> None:
    pending = state.get("pending_trajectory", [])
    if not pending:
        return
    now_ts = time.time()
    still = []
    for traj in pending:
        all_done = True
        for check in traj["checks"]:
            if check["price"] is not None:
                continue
            if now_ts < check["due_ts"]:
                all_done = False
                continue
            try:
                fi = yf.Ticker(traj["pair"]).fast_info
                price = float(fi.get("lastPrice", fi.get("last_price", 0)))
                check["price"] = round(price, 5)
            except Exception:
                all_done = False
        if all_done:
            _write_trajectory(traj)
        else:
            still.append(traj)
    state["pending_trajectory"] = still


def _write_trajectory(traj: dict) -> None:
    row = {
        "signal_ts": traj["signal_ts"], "pair": traj["pair"].replace("=X", ""),
        "direction": traj["direction"], "entry_price": traj["entry"],
        "strength_pct": traj["strength_pct"], "atr_ratio": traj["atr_ratio"],
        "strength_bucket": traj["strength_bucket"], "hour_utc": traj["hour_utc"],
        "day_of_week": traj["day_of_week"], "is_cpi_day": traj["is_cpi_day"],
    }
    for check in traj["checks"]:
        m = check["min"]
        row[f"p{m}m"] = check["price"]
        if check["price"] is None:
            row[f"res{m}m"] = "UNKNOWN"
        elif traj["direction"] == "BUY":
            row[f"res{m}m"] = "WIN" if check["price"] > traj["entry"] else "LOSS"
        else:
            row[f"res{m}m"] = "WIN" if check["price"] < traj["entry"] else "LOSS"

    new_file = not os.path.exists(TRAJECTORY_FILE)
    with open(TRAJECTORY_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRAJECTORY_FIELDS)
        if new_file: w.writeheader()
        w.writerow(row)


def check_tracking_health(state: dict) -> None:
    if not os.path.exists(TRAJECTORY_FILE): return
    age_hours = (time.time() - os.path.getmtime(TRAJECTORY_FILE)) / 3600
    if age_hours > TRACKING_HEALTH_HOURS and (time.time() - state.get("last_health_alert", 0)) > 86400:
        if send_telegram_message(f"⚠️ <b>Трекінг не пише {age_hours:.0f} год</b>"):
            state["last_health_alert"] = time.time()
    if age_hours > TRACKING_EMERGENCY_HOURS and (time.time() - state.get("last_emergency_alert", 0)) > 86400:
        if send_telegram_message(f"🚨 <b>ТРЕКІНГ МЕРТВИЙ >{TRACKING_EMERGENCY_HOURS} год</b>"):
            state["last_emergency_alert"] = time.time()


def send_weekly_tech_report(state: dict) -> None:
    now = datetime.now(TZ)
    if now.weekday() != WEEKLY_REPORT_DAY or now.hour != WEEKLY_REPORT_HOUR: return
    week_key = now.strftime("%Y-%W")
    if state.get("last_weekly_report") == week_key: return
    msg = f"📊 <b>Тижневий технічний звіт</b>\n⏰ {now.strftime('%Y-%m-%d %H:%M')} (Kyiv)"
    if send_telegram_message(msg): state["last_weekly_report"] = week_key


def send_daily_report(state: dict) -> None:
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    if state.get("last_daily_report") == today: return

    today_signals = []
    if os.path.exists(JOURNAL_FILE):
        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            today_signals = [r for r in csv.DictReader(f) if r.get("ts", "").startswith(today)]

    today_outcomes = []
    if os.path.exists(OUTCOMES_FILE):
        with open(OUTCOMES_FILE, "r", encoding="utf-8") as f:
            today_outcomes = [r for r in csv.DictReader(f) if r.get("ts", "").startswith(today)]

    wins = sum(1 for o in today_outcomes if o.get("result") == "WIN")
    losses = sum(1 for o in today_outcomes if o.get("result") == "LOSS")

    msg = f"📊 <b>Підсумок дня: {today}</b>\n📍 Сигналів: {len(today_signals)}\n✅ WIN: {wins} | ❌ LOSS: {losses}\n"
    if wins+losses > 0: msg += f"📈 Winrate: <b>{wins/(wins+losses)*100:.1f}%</b>\n"
    if send_telegram_message(msg): state["last_daily_report"] = today


# ============================================================
# FIX 48: НОВИЙ ДИЗАЙН ПОВІДОМЛЕНЬ
# ============================================================
def _vis_len(t: str) -> int:
    return sum(2 if ord(c) >= 0x2100 else 1 for c in t)


def color_frame(lines: list, sq: str) -> str:
    top = sq * ((max(_vis_len(t) for t in lines) + 5) // 2)
    return "\n".join([top, *[f"{sq} {t} {sq}" for t in lines], top])


def bar(value: float, vmax: float, size: int = 10) -> str:
    n = max(0, min(size, round(value / vmax * size)))
    return "█" * n + "░" * (size - n)


def metric(icon, value, vmax, name, val_txt, label):
    return f"<code>[{bar(value, vmax)}]</code> {icon} {name} <code>{val_txt}</code> — {label}"


def format_signal(pair: str, trend: str, price: float, level: dict, breakout: bool,
                  source: str, score: float, er: float,
                  er5: float = 0.0, loc_dir: str = "") -> str:
    clean_pair = pair.replace("=X", "")

    if score >= 80:
        strength_class = "strong"
    elif score >= 50:
        strength_class = "medium"
    elif score >= 30:
        strength_class = "weak"
    else:
        strength_class = "vweak"

    e, strength_label = STRENGTH[strength_class]
    c, arrow, dtx = DIRECTION[trend]
    sq = "🟩" if trend == "BUY" else "🟥"
    pct = round(score / MAX_SCORE * 100)

    zone_power = float(level["strength"]) * 100
    if zone_power < 5:
        zone_r = "vweak"
    elif zone_power < 10:
        zone_r = "weak"
    elif zone_power < 15:
        zone_r = "medium"
    else:
        zone_r = "strong"

    if er >= 0.45:
        trend_r = "strong"
    elif er >= 0.30:
        trend_r = "medium"
    elif er >= 0.22:
        trend_r = "weak"
    else:
        trend_r = "vweak"

    if er5 >= 0.30:
        pullback_r = "strong"
    elif er5 >= 0.15:
        pullback_r = "medium"
    elif er5 >= 0.10:
        pullback_r = "weak"
    else:
        pullback_r = "vweak"

    side = "підтримка" if trend == "BUY" else "опір"

    if level["level"] == 1:
        level_note = "перший ретест після пробою"
    elif level["level"] == 2:
        level_note = "перекриття х2 (серія після мінуса)"
    else:
        level_note = "фінальне перекриття (POC)"

    if zone_power < 5:
        advice = "зона дуже слабка за об'ємом — краще пропустити або чекати наступний рівень."
    elif zone_power < 10:
        advice = "зона слабка — не перекривай ставку."
    elif er < 0.20:
        advice = "тренд слабкий — обережно."
    elif er >= 0.30 and zone_power >= 10:
        advice = "сильний сетап — можна входити."
    else:
        advice = "нейтральний сетап — на твій розсуд."

    warning = ""
    if loc_dir:
        pullback = (loc_dir == "up" and trend == "SELL") or (loc_dir == "down" and trend == "BUY")
        with_sig = (loc_dir == "up" and trend == "BUY") or (loc_dir == "down" and trend == "SELL")
        if pullback:
            warning = "відкат ще імпульсний — може пробити зону, чекай реакції" if er5 >= 0.30 else "відкат вигас біля зони — гарні умови для входу"
        elif with_sig:
            warning = "ціна вже йде за сигналом — запізнення або пробій"

    if breakout:
        warning = "імпульсна свічка зараз — входь тільки в зоні, не женись за ціною"

    time_str = datetime.now(TZ).strftime("%H:%M")

    L = [
        color_frame([
            f"{c} {arrow} {clean_pair} · {dtx}",
            f"{e} {strength_label} · ⏱ {level['expiry_min']} хв · {time_str}",
        ], sq),
        f"📍 <b>Рівень {level['level']}</b> — {level_note}",
        f"💰 {side.capitalize()}: <code>{level['lo']:.5f} – {level['hi']:.5f}</code>",
        f"🎯 Вхід: <code>{price:.5f}</code>",
        DIV,
        f"📊 <b>ДАШБОРД СИГНАЛУ</b>",
        f"<code>[{bar(score, MAX_SCORE)}]</code> <b>Сила сигналу</b> <code>{score:.0f}/{MAX_SCORE}</code> · {pct}%",
        metric(ICON[zone_r], zone_power, SCALE["zone"], "Сила зони", f"{zone_power:.1f}%",
               "дуже слабка" if zone_r == "vweak" else "слабка" if zone_r == "weak" else "помірна" if zone_r == "medium" else "сильна"),
        metric(ICON[trend_r], er, SCALE["trend"], "Тренд M30", f"{er:.2f}",
               "слабкий" if trend_r == "vweak" else "помірний" if trend_r == "weak" else "сильний" if trend_r == "medium" else "дуже сильний"),
        metric(ICON[pullback_r], er5, SCALE["pullback"], "Відкат M5", f"{er5:.2f}",
               "слабкий" if pullback_r == "vweak" else "помірний" if pullback_r == "weak" else "сильний" if pullback_r == "medium" else "дуже сильний"),
    ]

    if warning:
        L.append(f"⚠️ <i>{warning}</i>")

    L += [
        DIV,
        f"💡 <b>Висновок:</b> {advice}",
        f"🕐 <i>{datetime.now(TZ).strftime('%d.%m.%Y %H:%M')} · Kyiv</i>"
    ]

    return "\n".join(L)


def journal_write(row: dict) -> None:
    new_file = not os.path.exists(JOURNAL_FILE)
    with open(JOURNAL_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
        if new_file: w.writeheader()
        w.writerow(row)


def outcome_write(row: dict) -> None:
    new_file = not os.path.exists(OUTCOMES_FILE)
    with open(OUTCOMES_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTCOME_FIELDS)
        if new_file: w.writeheader()
        w.writerow(row)


# ============================================================
# FIX 55: допоміжна функція логу сканування
# ============================================================
def _skip(pair: str, reason: str):
    SCAN_LOG.append((pair.replace("=X", ""), reason))
    return None


def evaluate_pair(pair: str, state: dict, news_events: list):
    clean = pair.replace("=X", "")

    # FIX 56: збираємо ВСІ події у вікні, попереджуємо по кожній, блокуємо пару
    matched = []
    for ev in news_events:
        if ev["currency"] in (clean[:3], clean[3:6]) and -NEWS_AFTER_MIN <= (ev["time"] - datetime.now(TZ)).total_seconds() / 60 <= NEWS_BEFORE_MIN:
            matched.append(ev)
    if matched:
        for ev in matched:
            maybe_send_news_warning(pair, ev, state)
        return _skip(pair, f"БЛОК НОВИНА {matched[0]['title']} ({matched[0]['time'].strftime('%H:%M')})")

    series_state = state.get(f"{pair}_series_state", "SCANNING")
    frozen_until = state.get(f"{pair}_frozen_until", 0)
    if time.time() < frozen_until:
        return _skip(pair, "заморожена після серії")

    df30 = fetch_candles(pair, interval="30m", period="30d")
    if df30.empty:
        return _skip(pair, "немає даних M30")
    trend, source, er = analyze_trend_30m(df30)
    if trend is None:
        return _skip(pair, f"— тренд M30: {source}")

    df5 = fetch_candles(pair, interval="5m", period="5d")
    if len(df5) < IMPULSE_LOOKBACK_5M:
        return _skip(pair, "мало даних M5")

    low, high, window = find_last_impulse(df5)
    impulse_range = high - low
    if impulse_range <= 0:
        return _skip(pair, "немає імпульсу на M5")

    current_price = float(df5["Close"].iloc[-1])
    pullback_depth = (high - current_price) / impulse_range if trend == "BUY" else (current_price - low) / impulse_range
    if pullback_depth < MIN_PULLBACK_FRACTION:
        return _skip(pair, f"{trend} — відкат замалий ({pullback_depth:.2f})")

    if state.get(f"{pair}_abandon_until", 0) > time.time():
        return _skip(pair, f"{trend} — abandon (POC пробито)")

    digits = 3 if "JPY" in pair else 4
    sig = f"{low:.{digits}f}|{high:.{digits}f}"
    if state.get(f"{pair}_done_sig") == sig:
        return _skip(pair, f"{trend} — імпульс уже відпрацьований")

    ladder_key = f"{pair}_ladder_data"
    sig_key = f"{pair}_ladder_sig"

    if state.get(sig_key) != sig:
        profile_window = get_daily_profile_window(df5)
        if profile_window.empty:
            return _skip(pair, "порожній денний профіль")
        new_ladder = build_entry_ladder(profile_window, trend, pair)
        if not new_ladder:
            return _skip(pair, f"{trend} — драбина не будується (зони/POC<10%/немає Л2/нерівномірна)")
        state[ladder_key] = new_ladder
        state[sig_key] = sig
        opposite = "SELL" if trend == "BUY" else "BUY"
        for k in [k for k in list(state.keys()) if k.startswith(f"{pair}_{opposite}_level")]:
            state.pop(k, None)

    ladder = state[ladder_key]
    if len(ladder) < 3:
        return _skip(pair, f"{trend} — драбина < 3 рівнів")

    l1, l2, l3 = ladder[0], ladder[1], ladder[2]

    if l3["strength"] < MIN_POC_STRENGTH:
        return _skip(pair, f"{trend} — POC слабкий ({l3['strength']*100:.1f}%)")

    if cooldown_active(state, pair):
        return _skip(pair, f"{trend} — кулдаун 3 хв")

    breakout = is_impulsive_breakout(df5, trend)
    atr_ratio = compute_atr_ratio(df5, 14, 20)
    now_dt = datetime.now(TZ)

    er5 = efficiency_ratio(df5["Close"], 24)
    if len(df5) >= 25:
        net5 = float(df5["Close"].iloc[-1] - df5["Close"].iloc[-25])
        loc_dir = "up" if net5 > 0 else ("down" if net5 < 0 else "flat")
    else:
        loc_dir = ""

    def calc_score(level):
        width = max(level["hi"] - level["lo"], 1e-12)
        depth = (level["hi"] - current_price) / width if trend == "BUY" else (current_price - level["lo"]) / width
        return round(300 * float(level["strength"]) + 20 * (1.0 - min(1.0, max(0.0, depth))) + (15 if level["is_poc"] else 0) + 25 * min(1.0, er / 0.5), 1)

    def cand(level, next_state):
        SCAN_LOG.append((clean, f"{trend} — КАНДИДАТ L{level['level']}!"))
        return {"pair": pair, "trend": trend, "source": source, "er": round(er, 3),
                "level": level, "entry": current_price, "score": calc_score(level), "breakout": breakout,
                "sig": sig, "atr_ratio": round(atr_ratio, 3),
                "er5": round(er5, 3), "loc_dir": loc_dir,
                "hour_utc": now_dt.astimezone(ZoneInfo("UTC")).hour, "day_of_week": now_dt.weekday(),
                "is_cpi_day": any("CPI" in ev.get("title", "") and ev.get("currency") == "USD" for ev in news_events),
                "next_state": next_state}

    if series_state == "SCANNING":
        if not in_zone(current_price, l1, trend):
            return _skip(pair, f"{trend} [SCANNING] — ціна {current_price:.5f} поза зоною L1 ({l1['lo']:.5f}–{l1['hi']:.5f})")
        if not approach_ok(df5, l1, trend):
            return _skip(pair, f"{trend} [SCANNING] — у зоні L1, але немає підходу")
        if zone_is_dwelling(df5, l1):
            return _skip(pair, f"{trend} [SCANNING] — ціна «сидить» у зоні L1")
        return cand(l1, "L1_ACTIVE")

    if series_state == "L1_ACTIVE":
        pending = next((o for o in state.get("pending_outcomes", []) if o["pair"] == pair and o.get("level") == 1), None)
        if not pending:
            return _skip(pair, f"{trend} [L1_ACTIVE] — немає відкритої угоди L1")
        if in_zone(current_price, l2, trend):
            return cand(l2, "L2_ACTIVE")
        return _skip(pair, f"{trend} [L1_ACTIVE] — ціна поза зоною L2 ({l2['lo']:.5f}–{l2['hi']:.5f})")

    if series_state == "L2_ACTIVE":
        pending = next((o for o in state.get("pending_outcomes", []) if o["pair"] == pair and o.get("level") == 2), None)
        if pending and in_zone(current_price, l3, trend):
            return cand(l3, "L3_ACTIVE")
        series_started = state.get(f"{pair}_series_started_at", 0)
        if time.time() - series_started > L2_TIMEOUT_MIN * 60:
            state[f"{pair}_frozen_until"] = time.time() + FREEZE_MIN * 60
            state[f"{pair}_series_state"] = "SCANNING"
            print(f"[v30.3] {pair}: таймер Л2 вийшов, серія закрита")
            return _skip(pair, f"{trend} — таймер Л2 вийшов, серія закрита")
        if zone_broken(l2, df5, trend):
            state[f"{pair}_frozen_until"] = time.time() + FREEZE_MIN * 60
            state[f"{pair}_series_state"] = "SCANNING"
            print(f"[v30.3] {pair}: Л2 пробито, серія закрита")
            return _skip(pair, f"{trend} — Л2 пробито, серія закрита")
        if not pending:
            return _skip(pair, f"{trend} [L2_ACTIVE] — немає відкритої угоди L2")
        return _skip(pair, f"{trend} [L2_ACTIVE] — ціна поза зоною L3 ({l3['lo']:.5f}–{l3['hi']:.5f})")

    if series_state == "WAITING_L3":
        waiting_since = state.get(f"{pair}_waiting_l3_since", 0)
        if time.time() - waiting_since > L3_WAIT_MIN * 60:
            state[f"{pair}_frozen_until"] = time.time() + FREEZE_MIN * 60
            state[f"{pair}_series_state"] = "SCANNING"
            print(f"[v30.3] {pair}: таймер Л3 вийшов, серія закрита")
            return _skip(pair, f"{trend} — таймер Л3 вийшов, серія закрита")
        if zone_broken(l2, df5, trend):
            state[f"{pair}_frozen_until"] = time.time() + FREEZE_MIN * 60
            state[f"{pair}_series_state"] = "SCANNING"
            print(f"[v30.3] {pair}: Л2 пробито під час очікування Л3, серія закрита")
            return _skip(pair, f"{trend} — Л2 пробито під час очікування Л3")
        if in_zone(current_price, l3, trend):
            return cand(l3, "L3_ACTIVE")
        return _skip(pair, f"{trend} [WAITING_L3] — ціна поза зоною L3 ({l3['lo']:.5f}–{l3['hi']:.5f})")

    if series_state == "L3_ACTIVE":
        pending = next((o for o in state.get("pending_outcomes", []) if o["pair"] == pair and o.get("level") == 3), None)
        if not pending:
            return _skip(pair, f"{trend} [L3_ACTIVE] — немає відкритої угоди L3")
        return _skip(pair, f"{trend} [L3_ACTIVE] — чекає результат POC")

    return _skip(pair, f"{trend} — невідомий стан {series_state}")


def run_cycle(state: dict, news_events: list = None) -> None:
    if news_events is None:
        news_events = fetch_high_impact_events()
    SCAN_LOG.clear()
    candidates = []
    for pair in PAIRS:
        try:
            cand = evaluate_pair(pair, state, news_events)
            if cand: candidates.append(cand)
        except Exception as e:
            SCAN_LOG.append((pair.replace("=X", ""), f"ПОМИЛКА: {e}"))
        time.sleep(BETWEEN_PAIRS_SLEEP_SEC)

    # FIX 55: постійний лог сканування — видно кожну пару і причину
    ts = datetime.now(TZ).strftime("%H:%M")
    print(f"[скан {ts}] старт: {len(PAIRS)} пар")
    for clean, reason in SCAN_LOG:
        print(f"   {clean}: {reason}")
    print(f"[скан {ts}] готово | кандидатів: {len(candidates)}")

    candidates.sort(key=lambda c: c["score"], reverse=True)
    sent_count = 0
    for cand in candidates:
        if sent_count >= MAX_SIGNALS_PER_RUN:
            break
        lvl = cand["level"]

        if lvl["level"] == 1 and l1_recent_count(state) >= MAX_L1_PER_HOUR:
            print(f"[ліміт] {cand['pair']}: L1 пропущено — ліміт {MAX_L1_PER_HOUR}/год")
            continue

        msg = format_signal(cand["pair"], cand["trend"], cand["entry"], lvl,
                            cand["breakout"], cand["source"], cand["score"],
                            cand["er"], cand.get("er5", 0.0), cand.get("loc_dir", ""))
        sent_ok = send_telegram_message(msg)

        if sent_ok:
            state[f"{cand['pair']}_{cand['trend']}_level{lvl['level']}_sent_at"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
            mark_signal_sent(state, cand["pair"])

            next_state = cand.get("next_state", "SCANNING")
            state[f"{cand['pair']}_series_state"] = next_state
            if next_state == "L2_ACTIVE":
                state[f"{cand['pair']}_series_started_at"] = time.time()
            elif next_state == "WAITING_L3":
                state[f"{cand['pair']}_waiting_l3_since"] = time.time()

            if lvl["is_poc"]:
                state[f"{cand['pair']}_done_sig"] = cand["sig"]
            state.setdefault("pending_outcomes", []).append({
                "pair": cand["pair"], "dir": cand["trend"], "entry": cand["entry"],
                "expiry_min": lvl["expiry_min"], "due_ts": time.time() + lvl["expiry_min"] * 60 + 90,
                "strength_pct": float(round(float(lvl["strength"]) * 100, 2)), "atr_ratio": cand.get("atr_ratio", 1.0),
                "level": lvl["level"], "sig": cand["sig"]})
            start_trajectory(state, cand, cand)
            sent_count += 1
            reason, sent_flag = "sent", True
        else:
            reason, sent_flag = "помилка надсилання", False

        journal_write({
            "ts": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"), "pair": cand["pair"].replace("=X", ""),
            "direction": cand["trend"], "level": lvl["level"], "zone_lo": float(round(lvl["lo"], 5)),
            "zone_hi": float(round(lvl["hi"], 5)), "entry_price": float(round(cand["entry"], 5)),
            "expiry_min": lvl["expiry_min"], "score": float(cand["score"]), "strength_pct": float(round(float(lvl["strength"]) * 100, 2)),
            "er": float(cand["er"]), "trend_source": cand["source"], "sent": sent_flag, "reason": reason,
            "atr_value": 0.0, "atr_ratio": float(cand.get("atr_ratio", 1.0)),
            "strength_bucket": classify_strength(float(lvl["strength"])), "hour_utc": cand.get("hour_utc", 0),
            "day_of_week": cand.get("day_of_week", 0), "is_cpi_day": cand.get("is_cpi_day", False)})


def process_outcomes(state: dict) -> None:
    pending = state.get("pending_outcomes", [])
    if not pending: return
    now_ts = time.time()
    still = []
    for o in pending:
        if o["due_ts"] > now_ts:
            still.append(o)
            continue
        try:
            fi = yf.Ticker(o["pair"]).fast_info
            exit_price = float(fi.get("lastPrice", fi.get("last_price", 0)))
            win = (exit_price > o["entry"]) if o["dir"] == "BUY" else (exit_price < o["entry"])
            outcome_write({"ts": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"), "pair": o["pair"].replace("=X", ""),
                           "direction": o["dir"], "entry_price": float(round(o["entry"], 5)),
                           "exit_price": float(round(exit_price, 5)), "result": "WIN" if win else "LOSS",
                           "strength_pct": o.get("strength_pct", ""), "atr_ratio": o.get("atr_ratio", "")})
            print(f"[результат] {o['pair']} {o['dir']} L{o.get('level')}: {'WIN' if win else 'LOSS'}")

            pair = o["pair"]
            if win:
                state[f"{pair}_frozen_until"] = time.time() + FREEZE_MIN * 60
                state[f"{pair}_series_state"] = "SCANNING"
                state[f"{pair}_done_sig"] = o.get("sig", "")
            else:
                level = o.get("level", 1)
                if level == 1:
                    state[f"{pair}_series_state"] = "L2_ACTIVE"
                    state[f"{pair}_series_started_at"] = time.time()
                elif level == 2:
                    state[f"{pair}_series_state"] = "WAITING_L3"
                    state[f"{pair}_waiting_l3_since"] = time.time()
                elif level == 3:
                    state[f"{pair}_frozen_until"] = time.time() + FREEZE_MIN * 60
                    state[f"{pair}_series_state"] = "SCANNING"
                    state[f"{pair}_done_sig"] = o.get("sig", "")

        except Exception:
            o["retries"] = o.get("retries", 0) + 1
            if o["retries"] < 5:
                o["due_ts"] = now_ts + 60
                still.append(o)
    state["pending_outcomes"] = still


def main_loop() -> None:
    state = load_state()
    while True:
        try:
            if not in_trading_window():
                time.sleep(OUT_OF_WINDOW_SLEEP_SEC)
                continue
            process_outcomes(state)
            process_trajectory(state)
            news_events = fetch_high_impact_events()
            run_cycle(state, news_events)
            print(f"[цикл] {datetime.now(TZ).strftime('%H:%M')} OK | відкритих серій: {len(state.get('pending_outcomes', []))} | новин: {len(news_events)}")
            send_daily_news_calendar(state)
            news_health_alert(state)
            if datetime.now(TZ).hour == DAILY_REPORT_HOUR and datetime.now(TZ).minute >= DAILY_REPORT_MINUTE:
                send_daily_report(state)
            check_tracking_health(state)
            send_weekly_tech_report(state)
            save_state(state)
        except Exception as e:
            print(f"[Помилка] {e}")
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    if RUN_MODE == "loop": main_loop()
    else:
        if in_trading_window():
            state = load_state(); process_outcomes(state); run_cycle(state); save_state(state)
