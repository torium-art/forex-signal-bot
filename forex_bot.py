"""
Forex Price-Action Signal Bot — v5

Специфікація трейдера:
  M30: локальний тренд за структурою останніх STRUCTURE_LOOKBACK (20-30)
       свічок; глобальний (EMA200) має співпадати з локальним, інакше пропуск.
       У консолідації ("болоті") сигнали не формуються.
  M5:  локальний імпульс ділиться на зони-подушки за бічним обсягом
       (наближення VRVP: обсяг свічки розподіляється по цінових комірках).
       Обсяг зони = "стіна": якщо закрита свічка опинилась ЗА зоною —
       стіна пробита, зона не працює. Пробій зони POC = злом:
       пара покидається на ABANDON_MIN хвилин.
  Драбина входів:
       Зона 1 = перший ретест після пробиття (найслабша) — 5 хв;
       Зона 2 = зона з дотиками свічок З ОБОХ боків (підтримка+опір) — 5 хв;
       Зона 3 = POC (найсильніша) — 10 хв, ОСТАННІЙ вхід на парі
       в цьому імпульсі (далі пара мовчить до нового імпульсу).
  Сигнал = ціна всередині зони + вхід з боку відкату + відбійна тінь
  на ЗАКРИТІЙ свічці 5m. За прогін <= MAX_SIGNALS_PER_RUN сигналів.

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

# --- Тренд M30 (спека: локальний 20-30 свічок + узгодженість з глобальним) ---
TREND_EMA_PERIOD = 200
STRUCTURE_LOOKBACK = 24            # 20-30 свічок M30 за спекою трейдера
REQUIRE_GLOBAL_AGREEMENT = True    # глобальний (EMA200) мусить збігатись з локальним
EFFICIENCY_MIN = 0.25              # нижче = флет/"пила"
NET_MOVE_ATR_MIN = 2.0             # чистий зсув за вікно >= 2 ATR

# --- Зони M5 (вузли обсягу, як VRVP) ---
POC_ZONES = 30
TOLERANCE_PCT = 0.00015            # буфер на межах зони для факту "входу"
IMPULSE_LOOKBACK_5M = 150
PEAK_MIN_REL = 0.50                # пік обсягу: комірка >= 50% від макс. комірки
VALLEY_FRACTION = 0.35             # розширення піка, поки сусіди >= 35% обсягу піка
MAX_ZONE_WIDTH_PCT = 0.002         # зона не ширша за 0.2% (ширше = виродження)
MIN_ZONE_TOUCHES = 4
MIN_TWO_SIDED = 1                  # мін. дотиків З КОЖНОГО боку для зони 2
MIN_WICK_RATIO = 0.35
EXTREME_AGE_MIN = 2                # екстремум імпульсу >= 2 свічок тому (відкат почався)

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
# ТРЕНД M30: локальний + узгодженість з глобальним + фільтр консолідації
# ---------------------------------------------------------------------------
def trend_by_ema(df: pd.DataFrame, period: int):
    if len(df) < period:
        return None
    ema = df["Close"].ewm(span=period, adjust=False).mean()
    return "BUY" if df["Close"].iloc[-1] > ema.iloc[-1] else "SELL"


def trend_by_structure(df: pd.DataFrame, lookback: int = STRUCTURE_LOOKBACK):
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
    """(trend, source, er). Локальний тренд = структура 20-30 свічок;
    глобальний = EMA200; торгуємо лише коли вони співпадають."""
    if len(df) < STRUCTURE_LOOKBACK + 1:
        return None, "мало даних 30m", 0.0
    atr = (df["High"] - df["Low"]).tail(14).mean()
    if atr <= 0:
        return None, "atr=0", 0.0
    er = efficiency_ratio(df["Close"], STRUCTURE_LOOKBACK)
    w = df.tail(STRUCTURE_LOOKBACK)
    net = w["Close"].iloc[-1] - w["Close"].iloc[0]

    if er < EFFICIENCY_MIN or abs(net) < NET_MOVE_ATR_MIN * atr:
        return None, "консолідація на 30m", er

    net_dir = "BUY" if net > 0 else "SELL"
    structure = trend_by_structure(df)
    if structure is None or structure != net_dir:
        return None, "немає чіткого локального тренда на 30m", er

    if REQUIRE_GLOBAL_AGREEMENT and len(df) >= TREND_EMA_PERIOD:
        ema_last = df["Close"].ewm(span=TREND_EMA_PERIOD, adjust=False).mean().iloc[-1]
        global_dir = "BUY" if df["Close"].iloc[-1] > ema_last else "SELL"
        if global_dir != structure:
            return None, "неузгодженість: локальний супроти глобального (EMA200)", er
    return structure, "structure+EMA200", er


# ---------------------------------------------------------------------------
# ЗОНИ M5: вузли обсягу (HVN) + дотики з обох боків
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


def build_volume_zones(cells):
    """Зони = вузли високого обсягу: пік + сусіди >= VALLEY_FRACTION від піка,
    ширина <= MAX_ZONE_WIDTH_PCT. LVN-комірки = межі зон."""
    vols = [c["vol"] for c in cells]
    total = sum(vols)
    if total <= 0:
        return []
    max_vol = max(vols)
    if max_vol <= 0:
        return []
    n = len(cells)
    peaks = []
    for i in range(n):
        if vols[i] < PEAK_MIN_REL * max_vol:
            continue
        left = vols[i - 1] if i > 0 else -1.0
        right = vols[i + 1] if i < n - 1 else -1.0
        if vols[i] >= left and vols[i] >= right:
            peaks.append(i)
    zones = []
    for p in peaks:
        lo_i, hi_i = p, p
        while (lo_i - 1 >= 0
               and vols[lo_i - 1] >= VALLEY_FRACTION * vols[p]
               and (cells[p]["hi"] - cells[lo_i - 1]["lo"]) / cells[p]["hi"] <= MAX_ZONE_WIDTH_PCT):
            lo_i -= 1
        while (hi_i + 1 < n
               and vols[hi_i + 1] >= VALLEY_FRACTION * vols[p]
               and (cells[hi_i + 1]["hi"] - cells[p]["lo"]) / cells[p]["lo"] <= MAX_ZONE_WIDTH_PCT):
            hi_i += 1
        zone_vol = sum(vols[lo_i:hi_i + 1])
        zones.append({
            "lo": cells[lo_i]["lo"], "hi": cells[hi_i]["hi"],
            "touches": sum(cells[i]["touches"] for i in range(lo_i, hi_i + 1)),
            "above": sum(cells[i]["above"] for i in range(lo_i, hi_i + 1)),
            "below": sum(cells[i]["below"] for i in range(lo_i, hi_i + 1)),
            "share": zone_vol / total,
        })
    zones.sort(key=lambda z: z["lo"])
    merged = []
    for z in zones:
        if merged and z["lo"] <= merged[-1]["hi"]:
            merged[-1]["hi"] = max(merged[-1]["hi"], z["hi"])
            merged[-1]["touches"] += z["touches"]
            merged[-1]["above"] += z["above"]
            merged[-1]["below"] += z["below"]
            merged[-1]["share"] += z["share"]
        else:
            merged.append(dict(z))
    return merged


def _mk_level(idx: int, z: dict, is_poc: bool) -> dict:
    return {"level": idx, "lo": z["lo"], "hi": z["hi"],
            "center": (z["lo"] + z["hi"]) / 2, "strength": z["share"],
            "above": z["above"], "below": z["below"], "is_poc": is_poc,
            "expiry_min": LEVEL_EXPIRY_POC_MIN if is_poc else LEVEL_EXPIRY_BASE_MIN}


def build_entry_ladder(window: pd.DataFrame, low: float, high: float, trend: str):
    """Драбина за спекою: зона 1 = перший ретест (найближча до краю імпульсу),
    зона 2 = зона з дотиками з обох боків, зона 3 = POC (найсильніша)."""
    cells = compute_volume_profile(window, low, high)
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
        f"({side})\n"
        f"Ціна входу: <code>{price:.5f}</code>\n"
        f"Експірація: {level['expiry_min']} хв\n"
        f"Тренд M30: {source} | сила зони: {level['strength'] * 100:.1f}% обсягу\n"
        f"Score: {score}"
        f"{warning}\n"
        f"Час: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')} (Kyiv)"
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


# ---------------------------------------------------------------------------
# НОВИНИ
# ---------------------------------------------------------------------------
def fetch_high_impact_events():
    try:
        resp = requests.get(NEWS_CALENDAR_URL, timeout=15)
        resp.raise_for_status()
        events = resp.json()
    except Exception as e:
        print(f"[Календар] помилка: {e}")
        return []
    out = []
    for ev in events:
        if ev.get("impact") != "High" or ev.get("country") not in WATCHED_CURRENCIES:
            continue
        try:
            ev_time = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(TZ)
        except Exception:
            continue
        out.append({"title": ev.get("title", "Подія"),
                    "currency": ev.get("country"), "time": ev_time})
    return out


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
           f"~{NEWS_AFTER_MIN} хв після виходу.")
    send_telegram_message(msg)
    state[key] = True


# ---------------------------------------------------------------------------
# ОЦІНКА ПАРИ -> КАНДИДАТ
# ---------------------------------------------------------------------------
def evaluate_pair(pair: str, state: dict, news_events):
    news_hit = find_relevant_news_window(pair, news_events)
    if news_hit:
        maybe_send_news_warning(pair, news_hit, state)
        print(f"{pair}: пропуск — поруч новина ({news_hit['title']})")
        return None

    df30 = fetch_candles(pair, interval="30m", period="30d")
    if df30.empty:
        return None
    trend, source, er = analyze_trend_30m(df30)
    if trend is None:
        print(f"{pair}: {source} — сигнали вимкнено")
        return None

    df5 = fetch_candles(pair, interval="5m", period="5d")
    if len(df5) < IMPULSE_LOOKBACK_5M:
        print(f"{pair}: недостатньо даних 5m")
        return None

    low, high, window = find_last_impulse(df5)
    if extreme_age(window, trend) < EXTREME_AGE_MIN:
        print(f"{pair}: імпульс ще свіжий, відкат не почався")
        return None

    # пара покинута після пробою зони ("злом")
    if state.get(f"{pair}_abandon_until", 0) > time.time():
        print(f"{pair}: пара покинула гру після пробою зони — чекаємо")
        return None

    ladder = build_entry_ladder(window, low, high, trend)
    if not ladder:
        print(f"{pair}: не вдалось побудувати зони")
        return None

    # пробій POC-зони = злом: лишаємо пару
    poc_zone = next(l for l in ladder if l["is_poc"])
    if zone_broken(poc_zone, df5, trend):
        state[f"{pair}_abandon_until"] = time.time() + ABANDON_MIN * 60
        print(f"{pair}: зону POC пробито свічкою — можливий злом, "
              f"лишаємо пару на {ABANDON_MIN} хв")
        return None

    # останній вхід (POC) у цьому імпульсі вже зроблено
    sig = f"{low:.5f}|{high:.5f}"
    if state.get(f"{pair}_done_sig") == sig:
        print(f"{pair}: входи цього імпульсу завершено (POC був останнім)")
        return None

    current_price = float(df5["Close"].iloc[-1])
    breakout = is_impulsive_breakout(df5, trend)

    if cooldown_active(state, pair):
        print(f"{pair}: кулдаун активний")
        return None

    candidate = None
    for level in ladder:
        if zone_broken(level, df5, trend):
            print(f"{pair}: зону {level['level']} пробито — стіна не спрацювала, пропуск")
            continue
        if not in_zone(current_price, level):
            continue
        if not approach_ok(df5, level, trend):
            print(f"{pair}: ціна в зоні {level['level']}, але вхід не з боку відкату")
            continue
        if not is_reaction_candle(df5, trend):
            print(f"{pair}: ціна в зоні {level['level']}, немає підтвердження відбою")
            continue
        if breakout and level["level"] > 1:
            print(f"{pair}: зона {level['level']} пропущена — імпульсний пробій")
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
        score = round(300 * level["strength"] + 20 * prox
                      + (15 if level["is_poc"] else 0) + 25 * clarity, 1)
        candidate = {"pair": pair, "trend": trend, "source": source,
                     "er": round(er, 3), "level": level, "entry": current_price,
                     "score": score, "breakout": breakout,
                     "signal_key": signal_key, "sig": sig}
        break

    if candidate is None:
        for level in ladder:
            state[f"{pair}_{trend}_level{level['level']}"] = False
        zones_str = [f"{l['lo']:.5f}-{l['hi']:.5f}" for l in ladder]
        print(f"{pair}: тренд={trend}({source}), ціна={current_price:.5f}, зони={zones_str}")
    return candidate


# ---------------------------------------------------------------------------
# ПРОГІН
# ---------------------------------------------------------------------------
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
        sent = sent_count < MAX_SIGNALS_PER_RUN
        reason = "sent" if sent else "ліміт сигналів за прогін"
        if sent:
            msg = format_signal(cand["pair"], cand["trend"], cand["entry"],
                                lvl, cand["breakout"], cand["source"], cand["score"])
            send_telegram_message(msg)
            state[cand["signal_key"]] = True
            mark_signal_sent(state, cand["pair"])
            if lvl["is_poc"]:
                state[f"{cand['pair']}_done_sig"] = cand["sig"]  # POC = останній вхід
            state.setdefault("pending_outcomes", []).append({
                "pair": cand["pair"], "dir": cand["trend"], "entry": cand["entry"],
                "expiry_min": lvl["expiry_min"],
                "due_ts": time.time() + lvl["expiry_min"] * 60 + 90,
            })
            sent_count += 1
            print(f"{cand['pair']}: сигнал відправлено "
                  f"(зона {lvl['level']}, score={cand['score']})")
        journal_write({
            "ts": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
            "pair": cand["pair"].replace("=X", ""),
            "direction": cand["trend"],
            "level": lvl["level"],
            "zone_lo": round(lvl["lo"], 5),
            "zone_hi": round(lvl["hi"], 5),
            "entry_price": round(cand["entry"], 5),
            "expiry_min": lvl["expiry_min"],
            "score": cand["score"],
            "strength_pct": round(lvl["strength"] * 100, 1),
            "er": cand["er"],
            "trend_source": cand["source"],
            "sent": sent,
            "reason": reason,
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
        exit_price = None
        try:
            fi = yf.Ticker(o["pair"]).fast_info
            try:
                exit_price = float(fi["lastPrice"])
            except Exception:
                exit_price = float(fi["last_price"])
        except Exception as e:
            print(f"[результат] не вдалось отримати ціну {o['pair']}: {e}")
            continue
        win = (exit_price > o["entry"]) if o["dir"] == "BUY" else (exit_price < o["entry"])
        outcome_write({
            "ts": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
            "pair": o["pair"].replace("=X", ""),
            "direction": o["dir"],
            "entry_price": round(o["entry"], 5),
            "exit_price": round(exit_price, 5),
            "result": "WIN" if win else "LOSS",
        })
        print(f"[результат] {o['pair']} {o['dir']}: {'WIN' if win else 'LOSS'}")
    state["pending_outcomes"] = still


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
def main_once() -> None:
    if not in_trading_window():
        print("Поза торговим вікном (10:00-20:00 Kyiv) — пропуск.")
        return
    state = load_state()
    run_cycle(state)
    save_state(state)


def main_loop() -> None:
    print("Бот запущено в безперервному режимі (loop).")
    print_outcome_stats()
    state = load_state()
    while True:
        try:
            if not in_trading_window():
                print("Поза торговим вікном — пауза.")
                time.sleep(OUT_OF_WINDOW_SLEEP_SEC)
                continue
            process_outcomes(state)
            run_cycle(state)
            save_state(state)
        except KeyboardInterrupt:
            print("Зупинено користувачем.")
            save_state(state)
            break
        except Exception as e:
            print(f"[Критична помилка циклу] {e}")
        time.sleep(CHECK_INTERVAL_SEC)


def main() -> None:
    if RUN_MODE == "loop":
        main_loop()
    else:
        main_once()


if __name__ == "__main__":
    main()
