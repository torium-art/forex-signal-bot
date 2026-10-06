"""
Forex Price-Action Signal Bot — v9

Повна специфікація трейдера + усі калібрування за результатами валідації
(21 пара на 30m + M5-зони проти VRVP):

  M30 (трендовий шар, v7):
    R1. Фільтр "болота" по вікну 48 свічок (24 год) з діагностикою
        (er, net*ATR) у рядку блокування. Поріг EFFICIENCY_MIN=0.15
        перекалібровано по доказах: рейнджі дають er <= 0.12,
        тренд+відкат >= 0.16 (v7.1).
    R2. Напрямок: структура 24 свічок -> структура 48 -> EMA200
        (якщо структури неоднозначні, а глобальний читанний).
    R3. Узгодженість з EMA200, коли глобальний читанний (>= 1 ATR).

  M5 (шар входу, v6-v9):
    Зони-подушки = вузли горизонтального обсягу (наближення VRVP):
    сегментація профілю по долинах LVN; зони неперетинні за побудовою.
    v8: профіль згладжується вікном 3 комірки перед пошуком долин,
    тому суцільний вузол не фрагментується на тонкі скибки.
    v9: VALLEY_FRACTION=0.15 — тонкі краї вузлів (40-60% піка) більше
    не ріжуться як долини, тому ВЕРХНЯ полиця вузла входить у зону:
    зона 1 сідає на перший ретест, а не на тіло вузла (кейс AUDJPY:
    зона піднялась з 110.118 до верхньої полиці ~110.18).
    Профіль будується по PROFILE_LOOKBACK_5M=350 свічок (~29 год),
    тому POC = справжня основа руху.
    Обсяг зони = "стіна": закрита свічка ЗА зоною = стіна пробита.
    Пробій зони POC = злом: пара покидається на ABANDON_MIN хв.
    Поглинання (v7): якщо ціна "живе" всередині зони (>= DWELL_MAX_CLOSES
    закриттів з DWELL_LOOKBACK) — це консолідація, не сетап.

  Драбина входів:
    Зона 1 = перший ретест після пробиття (найближча до краю імпульсу) — 5 хв;
    Зона 2 = зона з дотиками свічок З ОБОХ боків (підтримка+опір) — 5 хв;
    Зона 3 = POC (найсильніша) — 10 хв, ОСТАННІЙ вхід імпульсу
    (далі пара мовчить до нового імпульсу).
  Сигнал = ціна всередині зони + вхід з боку відкату + відбійна тінь
  на ЗАКРИТІЙ свічці 5m. Анти-дубль скидається ЛИШЕ коли ціна вийшла
  з зони. За прогін <= MAX_SIGNALS_PER_RUN сигналів (найкращі за score).

Режими (RUN_MODE): once (GitHub Actions) | loop (VPS/ПК).
Журнал: signals_journal.csv; результати: outcomes_journal.csv (loop).
"""
import csv
import json
import os
import time
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

TZ = ZoneInfo("Europe/Kyiv")
WORK_START = dtime(10, 0)
WORK_END = dtime(20, 0)

RUN_MODE = os.environ.get("RUN_MODE", "once")     # "once" | "loop"
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "60"))
BETWEEN_PAIRS_SLEEP_SEC = 1.0
OUT_OF_WINDOW_SLEEP_SEC = 300
MAX_SIGNALS_PER_RUN = 2

# --- Тренд M30 (v7: gate по 48 свічках, напрямок 24 -> 48 -> EMA200) ---
TREND_EMA_PERIOD = 200
STRUCTURE_LOOKBACK = 24            # локальна структура (12 год)
CONSOLIDATION_LOOKBACK = 48        # фільтр болота + середня структура (24 год)
REQUIRE_GLOBAL_AGREEMENT = True    # узгодженість з EMA200, коли глобальний читанний
GLOBAL_DIST_ATR = 1.0              # глобальний "читанний", якщо ціна >= 1 ATR від EMA200
EFFICIENCY_MIN = 0.15              # v7.1: перекалібровано по вікну 48 свічок:
                                   # рейнджі дають er <= 0.12, тренд+відкат >= 0.16
NET_MOVE_ATR_MIN = 2.0             # чистий зсув за 48 свічок >= 2 ATR

# --- Зони M5 (вузли обсягу, сегментація по долинах; v8-v9) ---
POC_ZONES = 30
TOLERANCE_PCT = 0.00015            # буфер на межах зони для факту "входу"
IMPULSE_LOOKBACK_5M = 150          # вікно імпульсу (край + свіжість)
PROFILE_LOOKBACK_5M = 350          # вікно профілю зон (~29 год)
VALLEY_FRACTION = 0.15             # v9: м'якший поріг долини після згладжування:
                                   # краї вузлів (40-60% піка) не ріжуться,
                                   # верхня полиця вузла входить у зону 1
MAX_ZONE_WIDTH_PCT = 0.002         # зона не ширша за 0.2% (ширше = ріжемо по LVN)
MIN_ZONE_TOUCHES = 4
MIN_TWO_SIDED = 1                  # мін. дотиків З КОЖНОГО боку для зони 2
MIN_WICK_RATIO = 0.35
EXTREME_AGE_MIN = 2                # екстремум імпульсу >= 2 свічок тому (відкат почався)

# --- Поглинання зони (v7) ---
DWELL_LOOKBACK = 12                # скільки закритих свічок перевіряємо (~1 год)
DWELL_MAX_CLOSES = 4               # максимум закриттів усередині зони, поки це "реакція"

# --- Сигнали / стіни ---
SIGNAL_COOLDOWN_MIN = 20
BREAKOUT_RANGE_MULT = 1.8
BREAKOUT_LOOKBACK = 20
LEVEL_EXPIRY_BASE_MIN = 5          # зони 1-2
LEVEL_EXPIRY_POC_MIN = 10          # POC (останній вхід)
ABANDON_MIN = 60                   # покинути пару на N хв після пробою POC-зони

# --- Новини ---
NEWS_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_BEFORE_MIN = 25
NEWS_AFTER_MIN = 20
WATCHED_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF"}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "last_signals.json")
JOURNAL_FILE = os.path.join(BASE_DIR, "signals_journal.csv")
OUTCOMES_FILE = os.path.join(BASE_DIR, "outcomes_journal.csv")

JOURNAL_FIELDS = ["ts", "pair", "direction", "level", "zone_lo", "zone_hi",
                  "entry_price", "expiry_min", "score", "strength_pct", "er",
                  "trend_source", "sent", "reason"]
OUTCOME_FIELDS = ["ts", "pair", "direction", "entry_price", "exit_price", "result"]


# ---------------------------------------------------------------------------
# ДОПОМІЖНІ
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# ТРЕНД M30 (v7: R1 gate 48, R2 напрямок 24->48->EMA200, R3 узгодженість)
# ---------------------------------------------------------------------------
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


def analyze_trend_30m(df: pd.DataFrame):
    """(trend, source, er).
    R1: болото-гейт по 48 свічках з діагностикою (er, net*ATR).
    R2: напрямок = структура24 (узгоджена з net24) -> структура48
        (узгоджена з net48) -> EMA200 (якщо читанна).
    R3: узгодженість з EMA200, коли глобальний читанний (>= 1 ATR)."""
    if len(df) < CONSOLIDATION_LOOKBACK + 1:
        return None, "мало даних 30m", 0.0
    atr = (df["High"] - df["Low"]).tail(14).mean()
    if atr <= 0:
        return None, "atr=0", 0.0

    # R1: фільтр болота по великому вікну
    er = efficiency_ratio(df["Close"], CONSOLIDATION_LOOKBACK)
    w48 = df.tail(CONSOLIDATION_LOOKBACK)
    net48 = w48["Close"].iloc[-1] - w48["Close"].iloc[0]
    if er < EFFICIENCY_MIN or abs(net48) < NET_MOVE_ATR_MIN * atr:
        return None, (f"консолідація на 30m (er={er:.2f}, "
                      f"net={abs(net48) / atr:.1f}*ATR)"), er

    # R2: напрямок
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
        return None, "немає чіткого тренда (структури 24/48 неоднозначні)", er

    # R3: узгодженість з глобальним
    if REQUIRE_GLOBAL_AGREEMENT and global_readable and trend != ema_dir:
        return None, f"неузгодженість: {source} супроти глобального (EMA200)", er
    return trend, source, er


# ---------------------------------------------------------------------------
# ЗОНИ M5: профіль обсягу + сегментація по долинах (v8: згладжування, v9: поріг)
# ---------------------------------------------------------------------------
def compute_volume_profile(window: pd.DataFrame, low: float, high: float):
    """Сирий профіль обсягу: комірки {lo, hi, vol, touches, above, below}.
    above = дотики ЗВЕРХУ (Low свічки всередині комірки),
    below = дотики ЗНИЗУ (High свічки всередині комірки)."""
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
        above[z_lo] += 1   # low свічки зайшов у комірку зверху
        below[z_hi] += 1   # high свічки зайшов у комірку знизу
    cells = []
    for i in range(POC_ZONES):
        cells.append({"lo": low + zone_size * i,
                      "hi": low + zone_size * (i + 1),
                      "vol": vol[i], "touches": touches[i],
                      "above": above[i], "below": below[i]})
    return cells


def split_wide_segment(idx, vols, cells, cap_pct):
    """Рекурсивно ріже широкий сегмент у точці найменшого обсягу (LVN-межа),
    поки шматки не вкладуться в cap_pct."""
    lo_i, hi_i = idx[0], idx[-1]
    width = (cells[hi_i]["hi"] - cells[lo_i]["lo"]) / cells[hi_i]["hi"]
    if width <= cap_pct or len(idx) <= 2:
        return [idx]
    interior = idx[1:-1]
    m = min(interior, key=lambda k: vols[k])
    left = [k for k in idx if k <= m]
    right = [k for k in idx if k > m]
    return (split_wide_segment(left, vols, cells, cap_pct)
            + split_wide_segment(right, vols, cells, cap_pct))


def build_volume_zones(cells):
    """Зони = вузли високого обсягу (HVN), розділені долинами малого
    обсягу (LVN), як у TradingView VRVP.
    v8: профіль згладжується вікном 3 комірки перед пошуком долин, тому
    суцільний вузол не фрагментується на тонкі скибки навколо пікових
    комірок. v9: поріг долини 0.15 — тонкі краї вузлів лишаються частиною
    вузла, тож верхня полиця входить у зону (зона 1 = перший ретест).
    Зони неперетинні за побудовою (ланцюгове злиття неможливе).
    Занадто широкий сегмент ріжеться по найслабших внутрішніх комірках."""
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
        width = (cells[j]["hi"] - cells[i]["lo"]) / cells[j]["hi"]
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


def build_entry_ladder(window_profile: pd.DataFrame, low: float, high: float,
                       trend: str):
    """Драбина за спекою: зона 1 = перший ретест (найближча до краю імпульсу),
    зона 2 = зона з дотиками з обох боків, зона 3 = POC (найсильніша).
    Профіль по PROFILE_LOOKBACK_5M, тому POC = справжня основа руху,
    а свіжі вузли = рівні 1-2."""
    cells = compute_volume_profile(window_profile, low, high)
    zones = [z for z in build_volume_zones(cells) if z["touches"] >= MIN_ZONE_TOUCHES]
    if not zones:
        return []
    poc = max(zones, key=lambda z: z["share"])
    poc_c = (poc["lo"] + poc["hi"]) / 2
    edge = high if trend == "BUY" else low
    others = [z for z in zones if z is not poc]
    if trend == "BUY":
        path = [z for z in others if poc_c <= (z["lo"] + z["hi"]) / 2 <= edge]
        path.sort(key=lambda z: -(z["lo"] + z["hi"]) / 2)
    else:
        path = [z for z in others if edge <= (z["lo"] + z["hi"]) / 2 <= poc_c]
        path.sort(key=lambda z: (z["lo"] + z["hi"]) / 2)

    ladder = []
    if path:
        ladder.append(_mk_level(len(ladder) + 1, path[0], False))     # зона 1
        two_sided = [z for z in path[1:]
                     if z["above"] >= MIN_TWO_SIDED and z["below"] >= MIN_TWO_SIDED]
        if two_sided:
            ladder.append(_mk_level(len(ladder) + 1, two_sided[0], False))  # зона 2
    ladder.append(_mk_level(len(ladder) + 1, poc, True))              # зона 3 = POC
    return ladder


def find_last_impulse(df: pd.DataFrame):
    window = df.tail(IMPULSE_LOOKBACK_5M)
    return window["Low"].min(), window["High"].max(), window


def extreme_age(window: pd.DataFrame, trend: str) -> int:
    if trend == "BUY":
        pos = window["High"].values.argmax()
    else:
        pos = window["Low"].values.argmin()
    return len(window) - 1 - pos


def in_zone(price: float, zone: dict) -> bool:
    return zone["lo"] * (1 - TOLERANCE_PCT) <= price <= zone["hi"] * (1 + TOLERANCE_PCT)


def approach_ok(df5: pd.DataFrame, zone: dict, trend: str) -> bool:
    """Вхід у зону з боку відкату: BUY — зверху, SELL — знизу."""
    if len(df5) < 4:
        return False
    recent = df5.iloc[-4:-1]
    if trend == "BUY":
        return recent["High"].max() >= zone["hi"]
    return recent["Low"].min() <= zone["lo"]


def zone_is_dwelling(df5: pd.DataFrame, zone: dict) -> bool:
    """v7: ціна 'живе' всередині зони (поглинає її) — це консолідація
    всередині стіни, а не реакція від неї."""
    if len(df5) < DWELL_LOOKBACK + 1:
        return False
    closes = df5["Close"].iloc[-(DWELL_LOOKBACK + 1):-1]  # лише закриті свічки
    inside = ((closes >= zone["lo"]) & (closes <= zone["hi"])).sum()
    return inside >= DWELL_MAX_CLOSES


def zone_broken(zone: dict, df5: pd.DataFrame, trend: str) -> bool:
    """'Стіна' пробита: закрита свічка 5m ЗА зоною. BUY: закриття нижче низу
    зони (підтримку зламано). SELL: закриття вище верху зони (опір зламано)."""
    if len(df5) < 2:
        return False
    c = df5.iloc[-2]
    if trend == "BUY":
        return c["Close"] < zone["lo"] * (1 - TOLERANCE_PCT)
    return c["Close"] > zone["hi"] * (1 + TOLERANCE_PCT)


def is_reaction_candle(df5: pd.DataFrame, trend: str) -> bool:
    """Реакція на ОСТАННІЙ ЗАКРИТІЙ свічці 5m: довга відбійна тінь."""
    if len(df5) < 2:
        return False
    c = df5.iloc[-2]
    rng = c["High"] - c["Low"]
    if rng <= 0:
        return False
    if trend == "BUY":
        wick = min(c["Open"], c["Close"]) - c["Low"]
    else:
        wick = c["High"] - max(c["Open"], c["Close"])
    return (wick / rng) >= MIN_WICK_RATIO


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


# ---------------------------------------------------------------------------
# TELEGRAM / ЖУРНАЛ
# ---------------------------------------------------------------------------
def send_telegram_message(html_text: str) -> None:
    if not in_trading_window():
        print("[Telegram] пропущено — поза торговим вікном")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": html_text,
                                    "parse_mode": "HTML"}, timeout=15)
    if resp.status_code != 200:
        print(f"[Telegram error] {resp.status_code}: {resp.text}")


def format_signal(pair: str, trend: str, price: float, level: dict,
                  breakout: bool, source: str, score: float) -> str:
    clean_pair = pair.replace("=X", "")
    direction = "🟢 BUY (CALL)" if trend == "BUY" else "🔴 SELL (PUT)"
    if level["is_poc"]:
        level_label = "Зона 3 / POC (найсильніша, ОСТАННІЙ вхід)"
    else:
        level_label = {1: "Зона 1 (перший ретест після пробиття)",
                       2: "Зона 2 (дотики з обох боків: підтримка+опір)"}.get(
            level["level"], f"Зона {level['level']}")
    side = "підтримка (вхід зверху)" if trend == "BUY" else "опір (вхід знизу)"
    warning = ""
    if breakout:
        warning = ("\n⚠️ Схоже на імпульсний пробій зони — "
                   "рекомендована ФІКСОВАНА ставка, без перекриттів.")
    return (
        f"<b>Сигнал: {clean_pair}</b>\n"
        f"Напрямок: {direction}\n"
        f"{level_label}\n"
        f"Зона: <code>{level['lo']:.5f}</code>–<code>{level['hi']:.5f}</code> "
        f"({
