"""
Forex Price-Action Signal Bot — v22
FIX 22: POC-інверсія
FIX 23: soft-log (strength_bucket, ATR, hour, day, CPI)
FIX 24: трекінг траєкторії (7 точок × 2 хв)
FIX 25: health-check + weekly tech report
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
MAX_ZONE_WIDTH_PCT = 0.0006
MIN_ZONE_TOUCHES = 4
MIN_TWO_SIDED = 1
MIN_WICK_RATIO = 0.35

MIN_PULLBACK_FRACTION = 0.10
MIN_SCORE = 30

DWELL_LOOKBACK = 12
DWELL_MAX_CLOSES = 6

SIGNAL_COOLDOWN_MIN = 20
BREAKOUT_RANGE_MULT = 1.8
BREAKOUT_LOOKBACK = 20
LEVEL_EXPIRY_BASE_MIN = 5
LEVEL_EXPIRY_POC_MIN = 10
ABANDON_MIN = 60

DAILY_REPORT_HOUR = 19
DAILY_REPORT_MINUTE = 55
WEEKLY_REPORT_DAY = 6   # неділя
WEEKLY_REPORT_HOUR = 20

NEWS_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_CACHE_TTL_HOURS = 4
NEWS_BEFORE_MIN = 25
NEWS_AFTER_MIN = 20
WATCHED_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF"}

# FIX 24: трекінг траєкторії — 7 точок кожні 2 хв
TRAJECTORY_POINTS = [2, 4, 6, 8, 10, 12, 14]   # хв

# FIX 25: health-check
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
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def fetch_candles(pair: str, interval: str, period: str) -> pd.DataFrame:
    df = yf.download(pair, interval=interval, period=period,
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna()


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """ATR за останні period свічок."""
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
    """ATR / середній ATR за останні avg_period значень ATR."""
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
    """Чи сьогодні день з US CPI (High impact)."""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    for ev in news_events:
        if ev.get("currency") != "USD":
            continue
        if "CPI" not in ev.get("title", ""):
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
        return [{"lo": mid, "hi": mid, "vol": 1.0, "touches": 1,
                 "above": 1, "below": 1}]
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
        cells.append({"lo": low + zone_size * i,
                      "hi": low + zone_size * (i + 1),
                      "vol": vol[i], "touches": touches[i],
                      "above": above[i], "below": below[i]})
    return cells


def split_wide_segment(idx, vols, cells, cap_pct):
    lo_i, hi_i = idx[0], idx[-1]
    denom = cells[hi_i]["hi"] or 1.0
    width = (cells[hi_i]["hi"] - cells[lo_i]["lo"]) / denom
    if width <= cap_pct or len(idx) <= 2:
        return [idx]
    interior = idx[1:-1]
    m = min(interior, key=lambda k: vols[k])
    left = [k for k in idx if k <= m]
    right = [k for k in idx if k > m]
    return (split_wide_segment(left, vols, cells, cap_pct)
            + split_wide_segment(right, vols, cells, cap_pct))


def build_volume_zones(cells):
    raw = [c["vol"] for c in cells]
    total = sum(raw)
    if total <= 0:
        return []
    n = len(cells)
    vols = []
    for i in range(n):
        lo = max(0, i - 1)
        hi = min(n, i + 2)
        vols.append(sum(raw[lo:hi]) / (hi - lo))
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
        denom = cells[j]["hi"] or 1.0
        width = (cells[j]["hi"] - cells[i]["lo"]) / denom
        if width > MAX_ZONE_WIDTH_PCT:
            pieces = split_wide_segment(seg, vols, cells, MAX_ZONE_WIDTH_PCT)
        else:
            pieces = [seg]
        for piece in pieces:
            lo_i, hi_i = piece[0], piece[-1]
            zone_vol = sum(raw[k] for k in piece)
            zones.append({
                "lo": cells[lo_i]["lo"], "hi": cells[hi_i]["hi"],
                "touches": sum(cells[k]["touches"] for k in piece),
                "above": sum(cells[k]["above"] for k in piece),
                "below": sum(cells[k]["below"] for k in piece),
                "share": zone_vol / total,
            })
        i = j + 1
    return zones


def _mk_level(idx: int, z: dict, is_poc: bool) -> dict:
    return {"level": idx, "lo": z["lo"], "hi": z["hi"],
            "center": (z["lo"] + z["hi"]) / 2, "strength": z["share"],
            "above": z["above"], "below": z["below"], "is_poc": is_poc,
            "expiry_min": LEVEL_EXPIRY_POC_MIN if is_poc else LEVEL_EXPIRY_BASE_MIN}


def build_entry_ladder(window_profile: pd.DataFrame, trend: str):
    """FIX 22: зони МІЖ POC і поточною ціною."""
    profile_low = window_profile["Low"].min()
    profile_high = window_profile["High"].max()
    cells = compute_volume_profile(window_profile, profile_low, profile_high)
    zones = [z for z in build_volume_zones(cells) if z["touches"] >= MIN_ZONE_TOUCHES]
    if not zones:
        return []
    poc = max(zones, key=lambda z: z["share"])
    poc_c = (poc["lo"] + poc["hi"]) / 2
    others = [z for z in zones if z is not poc]
    current_price = float(window_profile["Close"].iloc[-1])

    if trend == "BUY":
        if poc_c >= current_price:
            return []
        path = [z for z in others
                if poc_c <= (z["lo"] + z["hi"]) / 2 <= current_price]
        path.sort(key=lambda z: -(z["lo"] + z["hi"]) / 2)
    else:
        if poc_c <= current_price:
            return []
        path = [z for z in others
                if current_price <= (z["lo"] + z["hi"]) / 2 <= poc_c]
        path.sort(key=lambda z: (z["lo"] + z["hi"]) / 2)

    ladder = []
    if path:
        ladder.append(_mk_level(len(ladder) + 1, path[0], False))
        two_sided = [z for z in path[1:]
                     if z["above"] >= MIN_TWO_SIDED and z["below"] >= MIN_TWO_SIDED]
        if two_sided:
            ladder.append(_mk_level(len(ladder) + 1, two_sided[0], False))
    ladder.append(_mk_level(len(ladder) + 1, poc, True))
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


def send_telegram_message(html_text: str) -> bool:
    if not in_trading_window():
        print("[Telegram] пропущено — поза вікном")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": html_text,
                                    "parse_mode": "HTML"}, timeout=15)
    if resp.status_code != 200:
        print(f"[Telegram error] {resp.status_code}: {resp.text}")
        return False
    return True


# ============================================================
# FIX 24: ТРЕКІНГ ТРАЄКТОРІЇ
# ============================================================

def start_trajectory(state: dict, cand: dict, meta: dict) -> None:
    """Стартувати трекінг траєкторії для сигналу."""
    now_ts = time.time()
    checks = [{"min": m, "due_ts": now_ts + m * 60, "price": None}
              for m in TRAJECTORY_POINTS]
    state.setdefault("pending_trajectory", []).append({
        "pair": cand["pair"],
        "direction": cand["trend"],
        "entry": cand["entry"],
        "signal_ts": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        "strength_pct": float(round(float(cand["level"]["strength"]) * 100, 2)),
        "atr_ratio": meta.get("atr_ratio", 1.0),
        "strength_bucket": classify_strength(float(cand["level"]["strength"])),
        "hour_utc": meta.get("hour_utc", 0),
        "day_of_week": meta.get("day_of_week", 0),
        "is_cpi_day": meta.get("is_cpi_day", False),
        "checks": checks,
    })


def process_trajectory(state: dict) -> None:
    """Записати ціни для точок, які вже дозріли."""
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
            # час настав — тягнемо ціну
            try:
                fi = yf.Ticker(traj["pair"]).fast_info
                try:
                    price = float(fi["lastPrice"])
                except Exception:
                    price = float(fi["last_price"])
                check["price"] = round(price, 5)
            except Exception as e:
                print(f"[trajectory] {traj['pair']}: помилка {e}")
                all_done = False
        if all_done:
            # всі 7 точок зібрані — записати в CSV
            _write_trajectory(traj)
        else:
            still.append(traj)
    state["pending_trajectory"] = still


def _write_trajectory(traj: dict) -> None:
    """Записати завершену траєкторію в CSV."""
    row = {
        "signal_ts": traj["signal_ts"],
        "pair": traj["pair"].replace("=X", ""),
        "direction": traj["direction"],
        "entry_price": traj["entry"],
        "strength_pct": traj["strength_pct"],
        "atr_ratio": traj["atr_ratio"],
        "strength_bucket": traj["strength_bucket"],
        "hour_utc": traj["hour_utc"],
        "day_of_week": traj["day_of_week"],
        "is_cpi_day": traj["is_cpi_day"],
    }
    # ціни по точках
    for check in traj["checks"]:
        m = check["min"]
        row[f"p{m}m"] = check["price"]
        # результат: WIN якщо ціна в наш бік
        if check["price"] is None:
            row[f"res{m}m"] = "UNKNOWN"
        elif traj["direction"] == "BUY":
            row[f"res{m}m"] = "WIN" if check["price"] > traj["entry"] else "LOSS"
        else:
            row[f"res{m}m"] = "WIN" if check["price"] < traj["entry"] else "LOSS"

    new_file = not os.path.exists(TRAJECTORY_FILE)
    with open(TRAJECTORY_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRAJECTORY_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)
    print(f"[trajectory] записано {traj['pair']}")


# ============================================================
# FIX 25: HEALTH-CHECK + WEEKLY REPORT
# ============================================================

def check_tracking_health(state: dict) -> None:
    """Перевірити свіжість trajectory_journal.csv. Алерт якщо >24 год."""
    if not os.path.exists(TRAJECTORY_FILE):
        # файл ще не створений — це ОК на старті
        return
    mtime = os.path.getmtime(TRAJECTORY_FILE)
    age_hours = (time.time() - mtime) / 3600

    # Алерт раз на добу
    last_alert = state.get("last_health_alert", 0)
    if age_hours > TRACKING_HEALTH_HOURS and (time.time() - last_alert) > 86400:
        msg = (f"⚠️ <b>Трекінг не пише {age_hours:.0f} год</b>\n"
               f"Останній запис: {datetime.fromtimestamp(mtime, TZ).strftime('%Y-%m-%d %H:%M')}\n"
               f"Перевір бота в Termux.")
        if send_telegram_message(msg):
            state["last_health_alert"] = time.time()

    # Emergency stop
    if age_hours > TRACKING_EMERGENCY_HOURS:
        last_emergency = state.get("last_emergency_alert", 0)
        if (time.time() - last_emergency) > 86400:
            msg = (f"🚨 <b>ТРЕКІНГ МЕРТВИЙ >{TRACKING_EMERGENCY_HOURS} год</b>\n"
                   f"ПОТРІБНА ДІЯ. Перевір Termux і перезапусти бота.")
            if send_telegram_message(msg):
                state["last_emergency_alert"] = time.time()


def send_weekly_tech_report(state: dict) -> None:
    """Раз на тиждень (неділя 20:00) — технічний звіт."""
    now = datetime.now(TZ)
    if now.weekday() != WEEKLY_REPORT_DAY or now.hour != WEEKLY_REPORT_HOUR:
        return
    week_key = now.strftime("%Y-%W")
    if state.get("last_weekly_report") == week_key:
        return

    # Порахувати за тиждень
    week_ago = now - timedelta(days=7)
    total_signals = 0
    total_trajectories = 0
    if os.path.exists(JOURNAL_FILE):
        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    ts = datetime.strptime(row["ts"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
                    if ts >= week_ago:
                        total_signals += 1
                except Exception:
                    pass
    if os.path.exists(TRAJECTORY_FILE):
        with open(TRAJECTORY_FILE, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    ts = datetime.strptime(row["signal_ts"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
                    if ts >= week_ago:
                        total_trajectories += 1
                except Exception:
                    pass

    msg = (f"📊 <b>Тижневий технічний звіт</b>\n"
           f"━━━━━━━━━━━━━━━━━━━━━━\n"
           f"• Сигналів за тиждень: <b>{total_signals}</b>\n"
           f"• Траєкторій записано: <b>{total_trajectories}</b>\n"
           f"• Health: {'✅ OK' if total_trajectories > 0 else '⚠️ немає даних'}\n"
           f"━━━━━━━━━━━━━━━━━━━━━━\n"
           f"⏰ {now.strftime('%Y-%m-%d %H:%M')} (Kyiv)")
    if send_telegram_message(msg):
        state["last_weekly_report"] = week_key
        print(f"[weekly] відправлено за {week_key}")


def send_daily_report(state: dict) -> None:
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    if state.get("last_daily_report") == today:
        return

    today_signals = []
    if os.path.exists(JOURNAL_FILE):
        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("ts", "").startswith(today):
                    today_signals.append(row)

    today_outcomes = []
    if os.path.exists(OUTCOMES_FILE):
        with open(OUTCOMES_FILE, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("ts", "").startswith(today):
                    today_outcomes.append(row)

    wins = sum(1 for o in today_outcomes if o.get("result") == "WIN")
    losses = sum(1 for o in today_outcomes if o.get("result") == "LOSS")
    total = wins + losses

    # Скільки траєкторій за сьогодні
    today_trajectories = 0
    if os.path.exists(TRAJECTORY_FILE):
        with open(TRAJECTORY_FILE, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("signal_ts", "").startswith(today):
                    today_trajectories += 1

    msg = f"📊 <b>Підсумок дня: {today}</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📍 Сигналів: <b>{len(today_signals)}</b>\n"
    msg += f"✅ WIN: <b>{wins}</b>  |  ❌ LOSS: <b>{losses}</b>\n"
    if total > 0:
        wr = wins / total * 100
        emoji = "🔥" if wr >= 60 else "⚠️" if wr < 45 else "🟡"
        msg += f"📈 Winrate: {emoji} <b>{wr:.1f}%</b>\n"
    msg += f"🔬 Траєкторій: <b>{today_trajectories}</b>\n"

    if today_signals:
        msg += "━━━━━━━━━━━━━━━━━━━━━━\n"
        msg += "🎯 <b>Пари:</b>\n"
        pairs_count = {}
        for s in today_signals:
            p = s.get("pair", "")
            pairs_count[p] = pairs_count.get(p, 0) + 1
        for p, c in sorted(pairs_count.items(), key=lambda x: -x[1]):
            msg += f"• {p}: {c}\n"

    if today_outcomes:
        msg += "━━━━━━━━━━━━━━━━━━━━━━\n"
        msg += "📋 <b>Результати:</b>\n"
        for o in today_outcomes:
            mark = "✅" if o.get("result") == "WIN" else "❌"
            msg += f"{mark} {o.get('pair','')} {o.get('direction','')} {o.get('ts','')[11:]}\n"

    msg += "━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"⏰ {now.strftime('%H:%M')} (Kyiv)"

    if send_telegram_message(msg):
        state["last_daily_report"] = today
        print(f"[daily report] відправлено за {today}")


def format_signal(pair: str, trend: str, price: float, level: dict,
                  breakout: bool, source: str, score: float, er: float) -> str:
    clean_pair = pair.replace("=X", "")
    if trend == "BUY":
        direction = "🟢 BUY (CALL)"
        side = "підтримка"
    else:
        direction = "🔴 SELL (PUT)"
        side = "опір"

    if score >= 150:
        emoji, quality_word = "🔥", "ТОПОВИЙ"
    elif score >= 80:
        emoji, quality_word = "🟢", "СИЛЬНИЙ"
    elif score >= 50:
        emoji, quality_word = "🟡", "СЕРЕДНІЙ"
    else:
        emoji, quality_word = "🟠", "СЛАБКИЙ"

    if level["is_poc"]:
        level_label = "🎯 POC — останній рубіж (найсильніша зона)"
    elif level["level"] == 1:
        level_label = "1️⃣ Рівень 1 — перший ретест після пробою"
    else:
        level_label = f"🔁 Рівень {level['level']} — повторний ретест"

    s = float(level["strength"])
    if s >= 0.25:
        strength_word = "дуже сильна"
    elif s >= 0.15:
        strength_word = "сильна"
    elif s >= 0.10:
        strength_word = "середня"
    elif s >= 0.05:
        strength_word = "слабка"
    else:
        strength_word = "дуже слабка"

    if er >= 0.45:
        er_word = "дуже сильний тренд"
    elif er >= 0.30:
        er_word = "сильний тренд"
    elif er >= 0.22:
        er_word = "помірний тренд"
    else:
        er_word = "слабкий тренд"

    if source == "structure24":
        source_word = "свіжий тренд (24 св. M30)"
    elif source == "structure48":
        source_word = "старіший тренд (48 св. M30)"
    elif source == "ema200":
        source_word = "глобальний напрямок (EMA200)"
    else:
        source_word = source

    notes = []
    if s < 0.08:
        notes.append("зона слабка по об'єму — не перекривай ставку")
    if er >= 0.30:
        notes.append("тренд прямий і сильний — це плюс")
    elif er < 0.20:
        notes.append("тренд слабкий — обережно")
    if level["is_poc"]:
        notes.append("це POC — останній вхід перед можливим зломом тренду")
    if level["level"] == 1:
        notes.append("перший ретест — найменший ризик")
    if breakout:
        notes.append("⚠️ схоже на імпульсний пробій — ФІКСОВАНА ставка")
    if not notes:
        notes.append("сигнал стандартний")
    notes_text = "\n".join(f"• {n}" for n in notes)

    return (
        f"<b>{emoji} СИГНАЛ: {clean_pair} — {quality_word}</b>\n"
        f"Напрямок: {direction}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 {level_label}\n"
        f"💰 Зона {side}: <code>{level['lo']:.5f}</code> – <code>{level['hi']:.5f}</code>\n"
        f"💵 Ціна входу: <code>{price:.5f}</code>\n"
        f"⏱ Експірація: {level['expiry_min']} хв\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>ЯКІСТЬ СИГНАЛУ</b>\n"
        f"• Score: <b>{float(score):.0f}</b> (умовний макс ~150)\n"
        f"• Сила зони: <b>{s * 100:.1f}%</b> ({strength_word})\n"
        f"• Тренд: <b>{er:.2f}</b> ({er_word})\n"
        f"• Джерело: {source_word}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <b>ЩО ЦЕ ЗНАЧИТЬ</b>\n"
        f"{notes_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')} (Kyiv)"
    )


def journal_write(row: dict) -> None:
    new_file = not os.path.exists(JOURNAL_FILE)
    with open(JOURNAL_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)
    print(f"[журнал] {row}")


def outcome_write(row: dict) -> None:
    new_file = not os.path.exists(OUTCOMES_FILE)
    with open(OUTCOMES_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTCOME_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)


def print_outcome_stats() -> None:
    if not os.path.exists(OUTCOMES_FILE):
        return
    try:
        df = pd.read_csv(OUTCOMES_FILE)
    except Exception:
        return
    if df.empty:
        return
    wins = int((df["result"] == "WIN").sum())
    losses = int((df["result"] == "LOSS").sum())
    total = wins + losses
    if total:
        print(f"[статистика] результатів: {total}, WIN: {wins} ({wins / total:.0%}), LOSS: {losses}")


# ============================================================
# NEWS (кеш)
# ============================================================

def _load_news_cache():
    if not os.path.exists(NEWS_CACHE_FILE):
        return None
    try:
        with open(NEWS_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[Календар] помилка читання кешу: {e}")
        return None


def _save_news_cache(raw_data):
    try:
        cache = {"fetched_at": datetime.now(TZ).isoformat(), "data": raw_data}
        with open(NEWS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        print(f"[Календар] кеш оновлено ({len(raw_data)} подій)")
    except Exception as e:
        print(f"[Календар] помилка запису: {e}")


def _fetch_news_from_source():
    try:
        resp = requests.get(NEWS_CALENDAR_URL, timeout=15)
        if resp.status_code != 200:
            print(f"[Календар] HTTP {resp.status_code}")
            return None
        text = resp.text.strip()
        if not text.startswith("[") and not text.startswith("{"):
            print("[Календар] не JSON (можливо, rate limit)")
            return None
        return resp.json()
    except Exception as e:
        print(f"[Календар] помилка: {e}")
        return None


def _parse_events(raw_events):
    out = []
    for ev in raw_events:
        if ev.get("impact") != "High" or ev.get("country") not in WATCHED_CURRENCIES:
            continue
        try:
            ev_time = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(TZ)
        except Exception:
            continue
        out.append({"title": ev.get("title", "Подія"),
                    "currency": ev.get("country"), "time": ev_time})
    return out


def fetch_high_impact_events():
    now = datetime.now(TZ)
    cache = _load_news_cache()
    if cache is not None:
        fetched_at = cache.get("fetched_at")
        if fetched_at:
            try:
                fetched_dt = datetime.fromisoformat(fetched_at)
                age_hours = (now - fetched_dt).total_seconds() / 3600
                if age_hours < NEWS_CACHE_TTL_HOURS:
                    return _parse_events(cache.get("data", []))
            except Exception:
                pass
    raw = _fetch_news_from_source()
    if raw is not None:
        _save_news_cache(raw)
        return _parse_events(raw)
    if cache is not None:
        print("[Календар] оновлення не вдалось, використовуємо старий кеш")
        return _parse_events(cache.get("data", []))
    print("[Календар] немає даних")
    return []


def find_relevant_news_window(pair: str, events):
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
    key = f"news_{clean_pair}{event['title']}{event['time'].strftime('%Y%m%d%H%M')}"
    if state.get(key):
        return
    msg = (f"⚠️ <b>Скоро важлива новина: {clean_pair}</b>\n"
           f"Подія: {event['title']} ({event['currency']})\n"
           f"Час виходу: {event['time'].strftime('%H:%M')} (Kyiv)\n"
           f"Рекомендація: не торгувати ~{NEWS_BEFORE_MIN} хв до і "
           f"~{NEWS_AFTER_MIN} хв після.")
    if send_telegram_message(msg):
        state[key] = True


def evaluate_pair(pair: str, state: dict, news_events: list):
    news_hit = find_relevant_news_window(pair, news_events)
    if news_hit:
        maybe_send_news_warning(pair, news_hit, state)
        print(f"{pair}: пропуск — новина ({news_hit['title']})")
        return None

    df30 = fetch_candles(pair, interval="30m", period="30d")
    if df30.empty:
        return None
    trend, source, er = analyze_trend_30m(df30)
    if trend is None:
        print(f"{pair}: {source} — вимкнено")
        return None

    df5 = fetch_candles(pair, interval="5m", period="5d")
    if len(df5) < IMPULSE_LOOKBACK_5M:
        print(f"{pair}: мало даних 5m")
        return None

    low, high, window = find_last_impulse(df5)
    impulse_range = high - low
    if impulse_range <= 0:
        return None
    current_price_now = float(df5["Close"].iloc[-1])
    if trend == "BUY":
        pullback_depth = (high - current_price_now) / impulse_range
    else:
        pullback_depth = (current_price_now - low) / impulse_range
    if pullback_depth < MIN_PULLBACK_FRACTION:
        print(f"{pair}: відкат {pullback_depth * 100:.0f}% < 10%")
        return None

    if state.get(f"{pair}_abandon_until", 0) > time.time():
        print(f"{pair}: пара покинула гру")
        return None

    try:
        profile_window = df5.tail(PROFILE_LOOKBACK_5M)
        if not isinstance(profile_window, pd.DataFrame) or profile_window.empty:
            return None
        ladder = build_entry_ladder(profile_window, trend)
    except Exception as e:
        print(f"{pair}: помилка зон: {e}")
        return None

    if not ladder:
        print(f"{pair}: POC не з того боку ціни (FIX 22)")
        return None

    poc_zone = next(l for l in ladder if l["is_poc"])
    if zone_broken(poc_zone, df5, trend):
        state[f"{pair}_abandon_until"] = time.time() + ABANDON_MIN * 60
        print(f"{pair}: POC пробито — пауза {ABANDON_MIN} хв")
        return None

    sig = f"{low:.5f}|{high:.5f}"
    if state.get(f"{pair}_done_sig") == sig:
        print(f"{pair}: входи завершено")
        return None

    current_price = current_price_now
    breakout = is_impulsive_breakout(df5, trend)

    if cooldown_active(state, pair):
        print(f"{pair}: кулдаун")
        return None

    # FIX 23: обчислити ATR і метадані
    atr_ratio = compute_atr_ratio(df5, 14, 20)
    now_dt = datetime.now(TZ)
    hour_utc = now_dt.astimezone(ZoneInfo("UTC")).hour
    day_of_week = now_dt.weekday()
    is_cpi = is_cpi_day_today(news_events)

    candidate = None
    for level in ladder:
        if zone_broken(level, df5, trend):
            continue
        if zone_is_dwelling(df5, level):
            continue
        if not in_zone(current_price, level, trend):
            continue
        if not approach_ok(df5, level, trend):
            continue
        signal_key = f"{pair}_{trend}_level{level['level']}"
        if state.get(signal_key, False):
            continue
        width = max(level["hi"] - level["lo"], 1e-12)
        if trend == "BUY":
            depth = (level["hi"] - current_price) / width
        else:
            depth = (current_price - level["lo"]) / width
        prox = 1.0 - min(1.0, max(0.0, depth))
        clarity = min(1.0, er / 0.5)
        score = round(300 * float(level["strength"]) + 20 * prox
                      + (15 if level["is_poc"] else 0) + 25 * clarity, 1)
        if score < MIN_SCORE:
            continue
        candidate = {"pair": pair, "trend": trend, "source": source,
                     "er": round(er, 3), "level": level, "entry": current_price,
                     "score": score, "breakout": breakout,
                     "signal_key": signal_key, "sig": sig,
                     "atr_ratio": round(atr_ratio, 3),
                     "hour_utc": hour_utc,
                     "day_of_week": day_of_week,
                     "is_cpi_day": is_cpi}
        break

    if candidate is None:
        zones_str = [f"{l['lo']:.5f}-{l['hi']:.5f}" for l in ladder]
        print(f"{pair}: тренд={trend}, ціна={current_price:.5f}, зони={zones_str}")
    return candidate


def run_cycle(state: dict) -> None:
    news_events = fetch_high_impact_events()
    candidates = []
    for pair in PAIRS:
        try:
            cand = evaluate_pair(pair, state, news_events)
            if cand:
                candidates.append(cand)
        except Exception as e:
            print(f"[Помилка] {pair}: {e}")
        time.sleep(BETWEEN_PAIRS_SLEEP_SEC)

    candidates.sort(key=lambda c: c["score"], reverse=True)
    sent_count = 0
    for cand in candidates:
        lvl = cand["level"]
        allowed = sent_count < MAX_SIGNALS_PER_RUN
        sent_ok = False
        if allowed:
            msg = format_signal(cand["pair"], cand["trend"], cand["entry"],
                                lvl, cand["breakout"], cand["source"], cand["score"],
                                cand["er"])
            sent_ok = send_telegram_message(msg)
            if sent_ok:
                state[cand["signal_key"]] = True
                mark_signal_sent(state, cand["pair"])
                if lvl["is_poc"]:
                    state[f"{cand['pair']}_done_sig"] = cand["sig"]
                    prefix = f"{cand['pair']}_{cand['trend']}_level"
                    for k in [k for k in list(state.keys()) if k.startswith(prefix)]:
                        state.pop(k, None)
                state.setdefault("pending_outcomes", []).append({
                    "pair": cand["pair"], "dir": cand["trend"], "entry": cand["entry"],
                    "expiry_min": lvl["expiry_min"],
                    "due_ts": time.time() + lvl["expiry_min"] * 60 + 90,
                    "strength_pct": float(round(float(lvl["strength"]) * 100, 2)),
                    "atr_ratio": cand.get("atr_ratio", 1.0),
                })
                # FIX 24: старт трекінгу траєкторії
                start_trajectory(state, cand, {
                    "atr_ratio": cand.get("atr_ratio", 1.0),
                    "hour_utc": cand.get("hour_utc", 0),
                    "day_of_week": cand.get("day_of_week", 0),
                    "is_cpi_day": cand.get("is_cpi_day", False),
                })
                sent_count += 1
                print(f"{cand['pair']}: сигнал (зона {lvl['level']}, score={float(cand['score'])})")
            else:
                print(f"{cand['pair']}: помилка надсилання")
        if sent_ok:
            reason = "sent"
        elif allowed:
            reason = "помилка надсилання"
        else:
            reason = "ліміт за прогін"
        journal_write({
            "ts": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
            "pair": cand["pair"].replace("=X", ""),
            "direction": cand["trend"],
            "level": lvl["level"],
            "zone_lo": float(round(lvl["lo"], 5)),
            "zone_hi": float(round(lvl["hi"], 5)),
            "entry_price": float(round(cand["entry"], 5)),
            "expiry_min": lvl["expiry_min"],
            "score": float(cand["score"]),
            "strength_pct": float(round(float(lvl["strength"]) * 100, 2)),
            "er": float(cand["er"]),
            "trend_source": cand["source"],
            "sent": sent_ok,
            "reason": reason,
            "atr_value": float(round(compute_atr(fetch_candles(cand["pair"], "5m", "1d"), 14), 5)),
            "atr_ratio": float(cand.get("atr_ratio", 1.0)),
            "strength_bucket": classify_strength(float(lvl["strength"])),
            "hour_utc": cand.get("hour_utc", 0),
            "day_of_week": cand.get("day_of_week", 0),
            "is_cpi_day": cand.get("is_cpi_day", False),
        })


def process_outcomes(state: dict) -> None:
    pending = state.get("pending_outcomes", [])
    if not pending:
        return
    now_ts = time.time()
    still = []
    for o in pending:
        if o["due_ts"] > now_ts:
            still.append(o)
            continue
        try:
            fi = yf.Ticker(o["pair"]).fast_info
            try:
                exit_price = float(fi["lastPrice"])
            except Exception:
                exit_price = float(fi["last_price"])
        except Exception as e:
            retries = o.get("retries", 0) + 1
            if retries < 5:
                o["retries"] = retries
                o["due_ts"] = now_ts + 60
                still.append(o)
                print(f"[результат] {o['pair']}: спроба {retries}/5")
            else:
                outcome_write({
                    "ts": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
                    "pair": o["pair"].replace("=X", ""),
                    "direction": o["dir"],
                    "entry_price": float(round(o["entry"], 5)),
                    "exit_price": "",
                    "result": "UNKNOWN",
                    "strength_pct": o.get("strength_pct", ""),
                    "atr_ratio": o.get("atr_ratio", ""),
                })
            continue
        win = (exit_price > o["entry"]) if o["dir"] == "BUY" else (exit_price < o["entry"])
        outcome_write({
            "ts": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
            "pair": o["pair"].replace("=X", ""),
            "direction": o["dir"],
            "entry_price": float(round(o["entry"], 5)),
            "exit_price": float(round(exit_price, 5)),
            "result": "WIN" if win else "LOSS",
            "strength_pct": o.get("strength_pct", ""),
            "atr_ratio": o.get("atr_ratio", ""),
        })
        print(f"[результат] {o['pair']} {o['dir']}: {'WIN' if win else 'LOSS'}")
    state["pending_outcomes"] = still


def main_once() -> None:
    if not in_trading_window():
        print("Поза вікном (10:00-20:00 Kyiv) — пропуск.")
        return
    state = load_state()
    process_outcomes(state)
    process_trajectory(state)
    run_cycle(state)
    save_state(state)


def main_loop() -> None:
    print("Бот v22 запущено (loop).")
    print_outcome_stats()
    state = load_state()
    while True:
        try:
            if not in_trading_window():
                print("Поза вікном — пауза.")
                time.sleep(OUT_OF_WINDOW_SLEEP_SEC)
                continue
            process_outcomes(state)
            process_trajectory(state)
            run_cycle(state)

            now_check = datetime.now(TZ)
            if (now_check.hour == DAILY_REPORT_HOUR
                    and now_check.minute >= DAILY_REPORT_MINUTE):
                send_daily_report(state)

            # FIX 25: health-check і weekly report
            check_tracking_health(state)
            send_weekly_tech_report(state)

            save_state(state)
        except KeyboardInterrupt:
            print("Зупинено.")
            save_state(state)
            break
        except Exception as e:
            print(f"[Критична помилка] {e}")
        time.sleep(CHECK_INTERVAL_SEC)


def main() -> None:
    if RUN_MODE == "loop":
        main_loop()
    else:
        main_once()


if __name__ == "__main__":
    main()
