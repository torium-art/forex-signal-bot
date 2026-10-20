"""
Forex Price-Action Signal Bot — v28.1
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

SIGNAL_COOLDOWN_MIN = 3  # FIX 30: швидкий кулдаун для мартингейлу
BREAKOUT_RANGE_MULT = 1.8
BREAKOUT_LOOKBACK = 20

# FIX 31: анти-спам
WIN_PAUSE_MIN = 35       # пауза по парі після будь-якого WIN
LEVEL_REPEAT_MIN = 120   # той самий рівень не частіше ніж раз на 2 години
MAX_L1_PER_HOUR = 4      # максимум нових L1 за годину по всіх парах

# Логіка експірації
LEVEL_EXPIRY_BASE_MIN = 5   # Рівень 1
LEVEL_EXPIRY_MID_MIN = 15   # Рівень 2
LEVEL_EXPIRY_POC_MIN = 15   # POC

ABANDON_MIN = 30  # пауза після втрати на POC

DAILY_REPORT_HOUR = 19
DAILY_REPORT_MINUTE = 55
WEEKLY_REPORT_DAY = 6
WEEKLY_REPORT_HOUR = 20

NEWS_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_CACHE_TTL_HOURS = 4
NEWS_BEFORE_MIN = 25
NEWS_AFTER_MIN = 20
WATCHED_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF"}

TRAJECTORY_POINTS = [2, 4, 6, 8, 10, 12, 14]
TRACKING_HEALTH_HOURS = 24
TRACKING_EMERGENCY_HOURS = 48

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


def build_entry_ladder(window_profile: pd.DataFrame, trend: str):
    profile_low = window_profile["Low"].min()
    profile_high = window_profile["High"].max()
    atr = compute_atr(window_profile, 14)
    if atr <= 0:
        return []
    max_width = MAX_ZONE_WIDTH_ATR * atr
    cells = compute_volume_profile(window_profile, profile_low, profile_high)
    zones = [z for z in build_volume_zones(cells, max_width) if z["touches"] >= MIN_ZONE_TOUCHES]
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

    ladder = []
    if path:
        ladder.append(_mk_level(len(ladder) + 1, path[0], False))
        two_sided = [z for z in path[1:] if z["above"] >= MIN_TWO_SIDED and z["below"] >= MIN_TWO_SIDED]
        if two_sided:
            ladder.append(_mk_level(len(ladder) + 1, two_sided[0], False))
    ladder.append(_mk_level(len(ladder) + 1, poc, True))

    if len(ladder) < MIN_LADDER_ZONES:
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
    """FIX 31: скільки нових L1 відправлено за останню годину."""
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
# FIX 32 + FIX 34: ІНФОРМАТИВНИЙ ФОРМАТ ПОВІДОМЛЕННЯ
# ============================================================
def format_signal(pair: str, trend: str, price: float, level: dict, breakout: bool,
                  source: str, score: float, er: float,
                  er5: float = 0.0, loc_dir: str = "") -> str:
    clean_pair = pair.replace("=X", "")

    # Клас сигналу по score — видно з першого рядка
    if score >= 80:
        head_emoji, quality = "🟢", "СИЛЬНИЙ"
    elif score >= 50:
        head_emoji, quality = "🟡", "СЕРЕДНІЙ"
    else:
        head_emoji, quality = "🟠", "СЛАБКИЙ"

    direction = "🟢 BUY (CALL)" if trend == "BUY" else "🔴 SELL (PUT)"

    if level["level"] == 1:
        level_label = "1️⃣ Рівень 1 — перший ретест після пробою"
    elif level["level"] == 2:
        level_label = "2️⃣ Рівень 2 — перекриття х2 (якщо L1 був LOSS)"
    else:
        level_label = "3️⃣ Рівень 3 / POC — фінальне перекриття"

    side = "підтримка" if trend == "BUY" else "опір"

    s = float(level["strength"])
    if s >= 0.15: s_word = "сильна"
    elif s >= 0.10: s_word = "середня"
    elif s >= 0.05: s_word = "слабка"
    else: s_word = "дуже слабка"

    if er >= 0.45: er_word = "дуже сильний"
    elif er >= 0.30: er_word = "сильний"
    elif er >= 0.22: er_word = "помірний"
    else: er_word = "слабкий"

    if er5 >= 0.30: er5_word = "сильний"
    elif er5 >= 0.15: er5_word = "помірний"
    else: er5_word = "слабкий"

    src_word = {"structure24": "свіжий (24 св. M30)",
                "structure48": "старіший (48 св. M30)",
                "ema200": "глобальний (EMA200)"}.get(source, source)

    # FIX 34: стан ВІДКАТУ M5, а не "проти сигналу"
    loc_line = ""
    if loc_dir:
        pullback = (loc_dir == "up" and trend == "SELL") or (loc_dir == "down" and trend == "BUY")
        with_sig = (loc_dir == "up" and trend == "BUY") or (loc_dir == "down" and trend == "SELL")
        if pullback:
            tail = ("⚠️ ще імпульсний — може пробити зону, чекай реакції"
                    if er5 >= 0.30 else
                    "✅ вигас біля зони — гарні умови для входу")
        elif with_sig:
            tail = "⚠️ ціна вже йде за сигналом — запізнення або пробій"
        else:
            tail = "➖ флет"
        loc_line = f"• Відкат M5: {er5:.2f} ({er5_word}) · {tail}"

    # Блок висновків
    notes = []
    if s < 0.05:
        notes.append("зона дуже слабка по об'єму — краще пропустити або чекати наступний рівень")
    elif s < 0.10:
        notes.append("зона слабка по об'єму — не перекривай ставку")
    if er < 0.20:
        notes.append("тренд M30 слабкий — обережно")
    elif er >= 0.30:
        notes.append("тренд M30 прямий і сильний — це плюс")
    if loc_dir and er5 >= 0.30 and ((loc_dir == "up" and trend == "SELL") or (loc_dir == "down" and trend == "BUY")):
        notes.append("відкат на M5 ще імпульсний — зону може пробити, краще дочекатись реакції")
    if breakout:
        notes.append("імпульсна свічка зараз — входь тільки в зоні, не женись за ціною")
    if level["level"] == 2:
        notes.append("входь тільки якщо L1 був LOSS (перекриття х2)")
    elif level["level"] == 1:
        notes.append("перший ретест — найменший ризик")
    elif level["is_poc"]:
        notes.append("це POC — останній вхід перед можливим зломом тренду")
    notes = notes[:4]
    notes_block = "\n".join(f"• {n}" for n in notes) if notes else "• особливих зауважень немає"

    lines = [
        f"{head_emoji} <b>СИГНАЛ: {clean_pair} — {quality}</b>",
        f"Напрямок: {direction}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📍 <b>{level_label}</b>",
        f"💰 Зона {side}: <code>{level['lo']:.5f} – {level['hi']:.5f}</code>",
        f"💵 Вхід: <code>{price:.5f}</code>",
        f"⏱ Експірація: {level['expiry_min']} хв",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "📊 <b>ЯКІСТЬ СИГНАЛУ</b>",
        f"• Score: {score:.0f} (умовний макс ~150)",
        f"• Сила зони: {s*100:.1f}% ({s_word})",
        f"• Тренд M30: {er:.2f} ({er_word}) · {src_word}",
    ]
    if loc_line:
        lines.append(loc_line)
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━",
        "💡 <b>ЩО ЦЕ ЗНАЧИТЬ</b>",
        notes_block,
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"⏰ {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')} (Kyiv)",
    ]
    return "\n".join(lines)


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
                return [{"title": e["title"], "currency": e["country"], "time": datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(TZ)} for e in cache["data"] if e.get("impact") == "High" and e.get("country") in WATCHED_CURRENCIES]
        except Exception: pass

    try:
        resp = requests.get(NEWS_CALENDAR_URL, timeout=15)
        if resp.status_code == 200:
            raw = resp.json()
            with open(NEWS_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": now.isoformat(), "data": raw}, f)
            return [{"title": e["title"], "currency": e["country"], "time": datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(TZ)} for e in raw if e.get("impact") == "High" and e.get("country") in WATCHED_CURRENCIES]
    except Exception: pass
    return []


def evaluate_pair(pair: str, state: dict, news_events: list):
    for ev in news_events:
        clean = pair.replace("=X", "")
        if ev["currency"] in (clean[:3], clean[3:6]) and -NEWS_AFTER_MIN <= (ev["time"] - datetime.now(TZ)).total_seconds() / 60 <= NEWS_BEFORE_MIN:
            return None

    df30 = fetch_candles(pair, interval="30m", period="30d")
    if df30.empty: return None
    trend, source, er = analyze_trend_30m(df30)
    if trend is None: return None

    df5 = fetch_candles(pair, interval="5m", period="5d")
    if len(df5) < IMPULSE_LOOKBACK_5M: return None

    low, high, window = find_last_impulse(df5)
    impulse_range = high - low
    if impulse_range <= 0: return None

    current_price = float(df5["Close"].iloc[-1])
    pullback_depth = (high - current_price) / impulse_range if trend == "BUY" else (current_price - low) / impulse_range
    if pullback_depth < MIN_PULLBACK_FRACTION: return None

    if state.get(f"{pair}_abandon_until", 0) > time.time(): return None

    # FIX 31: стабільний ключ драбини — округлення до 0.1 піпса
    digits = 3 if "JPY" in pair else 4
    sig = f"{low:.{digits}f}|{high:.{digits}f}"
    if state.get(f"{pair}_done_sig") == sig: return None

    ladder_key = f"{pair}_ladder_data"
    sig_key = f"{pair}_ladder_sig"

    if state.get(sig_key) != sig:
        profile_window = df5.tail(PROFILE_LOOKBACK_5M)
        if profile_window.empty: return None
        new_ladder = build_entry_ladder(profile_window, trend)
        if not new_ladder: return None
        state[ladder_key] = new_ladder
        state[sig_key] = sig
        # FIX 31: чистимо позначки рівнів ТІЛЬКИ протилежного напрямку
        opposite = "SELL" if trend == "BUY" else "BUY"
        for k in [k for k in list(state.keys()) if k.startswith(f"{pair}_{opposite}_level")]:
            state.pop(k, None)

    ladder = state[ladder_key]
    poc_zone = next(l for l in ladder if l["is_poc"])
    if zone_broken(poc_zone, df5, trend):
        state[f"{pair}_abandon_until"] = time.time() + ABANDON_MIN * 60
        state.pop(ladder_key, None)
        state.pop(sig_key, None)
        return None

    if cooldown_active(state, pair): return None

    breakout = is_impulsive_breakout(df5, trend)
    atr_ratio = compute_atr_ratio(df5, 14, 20)
    now_dt = datetime.now(TZ)

    # FIX 32: локальний тренд M5 для повідомлення
    er5 = efficiency_ratio(df5["Close"], 24)
    if len(df5) >= 25:
        net5 = float(df5["Close"].iloc[-1] - df5["Close"].iloc[-25])
        loc_dir = "up" if net5 > 0 else ("down" if net5 < 0 else "flat")
    else:
        loc_dir = ""

    for level in ladder:
        if zone_broken(level, df5, trend): continue
        if zone_is_dwelling(df5, level): continue
        if not in_zone(current_price, level, trend): continue
        if not approach_ok(df5, level, trend): continue

        # FIX 31: той самий рівень не частіше ніж раз на LEVEL_REPEAT_MIN
        signal_key = f"{pair}_{trend}_level{level['level']}_sent_at"
        sent_at = state.get(signal_key)
        if sent_at:
            try:
                sent_dt = datetime.strptime(sent_at, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
                if (datetime.now(TZ) - sent_dt).total_seconds() / 60 < LEVEL_REPEAT_MIN:
                    continue
            except Exception:
                continue

        width = max(level["hi"] - level["lo"], 1e-12)
        depth = (level["hi"] - current_price) / width if trend == "BUY" else (current_price - level["lo"]) / width
        score = round(300 * float(level["strength"]) + 20 * (1.0 - min(1.0, max(0.0, depth))) + (15 if level["is_poc"] else 0) + 25 * min(1.0, er / 0.5), 1)

        if score < MIN_SCORE: continue

        return {"pair": pair, "trend": trend, "source": source, "er": round(er, 3),
                "level": level, "entry": current_price, "score": score, "breakout": breakout,
                "sig": sig, "atr_ratio": round(atr_ratio, 3),
                "er5": round(er5, 3), "loc_dir": loc_dir,
                "hour_utc": now_dt.astimezone(ZoneInfo("UTC")).hour, "day_of_week": now_dt.weekday(),
                "is_cpi_day": any("CPI" in ev.get("title", "") and ev.get("currency") == "USD" for ev in news_events)}
    return None


def run_cycle(state: dict) -> None:
    news_events = fetch_high_impact_events()
    candidates = []
    for pair in PAIRS:
        try:
            cand = evaluate_pair(pair, state, news_events)
            if cand: candidates.append(cand)
        except Exception:
            pass
        time.sleep(BETWEEN_PAIRS_SLEEP_SEC)

    candidates.sort(key=lambda c: c["score"], reverse=True)
    sent_count = 0
    for cand in candidates:
        if sent_count >= MAX_SIGNALS_PER_RUN:
            break
        lvl = cand["level"]

        # FIX 31: ліміт нових L1 за годину (каскади L2/POC не обмежуємо)
        if lvl["level"] == 1 and l1_recent_count(state) >= MAX_L1_PER_HOUR:
            print(f"[ліміт] {cand['pair']}: L1 пропущено — ліміт {MAX_L1_PER_HOUR}/год")
            continue

        msg = format_signal(cand["pair"], cand["trend"], cand["entry"], lvl,
                            cand["breakout"], cand["source"], cand["score"],
                            cand["er"], cand.get("er5", 0.0), cand.get("loc_dir", ""))
        sent_ok = send_telegram_message(msg)

        # FIX 33: журнал пише тільки те, що реально відбулось
        if sent_ok:
            state[f"{cand['pair']}_{cand['trend']}_level{lvl['level']}_sent_at"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
            mark_signal_sent(state, cand["pair"])
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

            if win:
                # FIX 31: будь-який WIN закриває серію І ставить паузу по парі
                state[f"{o['pair']}_done_sig"] = o.get("sig", "")
                state[f"{o['pair']}_abandon_until"] = time.time() + WIN_PAUSE_MIN * 60
            elif o.get("level") == 3:
                # LOSS на POC — серія завершена, пауза
                state[f"{o['pair']}_done_sig"] = o.get("sig", "")
                state[f"{o['pair']}_abandon_until"] = time.time() + ABANDON_MIN * 60
            # LOSS на L1/L2 — нічого не ставимо: серія чекає наступного рівня

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
            run_cycle(state)
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
