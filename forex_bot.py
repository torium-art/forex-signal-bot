"""
Forex Price-Action Signal Bot — v3

Головна відмінність від v2: рівні входу — це ЗОНИ (цінові діапазони [lo, hi]),
як їх позначає трейдер на графіку, а не тонкі лінії. Зона працює як підтримка,
коли ціна заходить у неї зверху (BUY), і як опір — коли знизу (SELL).
Сигнал = ціна всередині зони + вхід з боку відкату + реакція (відбійна тінь
на ЗАКРИТІЙ свічці 5m).

Логіка загалом:
  M30: чіткий тренд (локальний структурний АБО глобальний EMA200).
       У консолідації сигнали НЕ формуються. Конфлікт локального і
       глобального напрямку = невизначеність = пропуск.
  M5:  імпульс -> зони горизонтального обсягу (наближення VRVP: обсяг свічки
       розподіляється по цінових зонах, які перекриває її High-Low).
       POC = зона з максимальним обсягом.
       Драбина: зона 1 (1-й дотик, 5 хв), зона 2 (глибше, 5 хв),
       POC (10 хв).
  За прогін <= MAX_SIGNALS_PER_RUN сигналів, найкращі за score.

Режими (змінна RUN_MODE): once (GitHub Actions) | loop (VPS/ПК).
Журнал: signals_journal.csv, результати: outcomes_journal.csv (loop).
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

# --- Тренд M30 ---
TREND_EMA_PERIOD = 200
STRUCTURE_LOOKBACK = 40
EFFICIENCY_MIN = 0.25            # нижче = флет/"пила"
NET_MOVE_ATR_MIN = 2.0           # чистий зсув за вікно >= 2 ATR
GLOBAL_DIST_ATR = 1.0            # глобальний тренд чіткий, якщо ціна >= 1 ATR від EMA200

# --- Зони M5 ---
POC_ZONES = 30
TOLERANCE_PCT = 0.00015          # буфер на межах зони для факту "входу"
IMPULSE_LOOKBACK_5M = 150
ZONE_MERGE_PCT = 0.0015          # зони з проміжком < 0.15% зливаються в одну смугу
MIN_ZONE_TOUCHES = 4
MIN_WICK_RATIO = 0.35
EXTREME_AGE_MIN = 2              # екстремум імпульсу >= 2 свічок тому (відкат почався)

# --- Сигнали ---
SIGNAL_COOLDOWN_MIN = 20
BREAKOUT_RANGE_MULT = 1.8
BREAKOUT_LOOKBACK = 20
LEVEL_EXPIRY_BASE_MIN = 5        # зони 1-2
LEVEL_EXPIRY_POC_MIN = 10        # POC

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
# ТРЕНД M30 + ФІЛЬТР КОНСОЛІДАЦІЇ
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
    """Повертає (trend, source, er). None = консолідація/невизначеність."""
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
    if structure is not None and structure != net_dir:
        structure = None

    ema_series = df["Close"].ewm(span=TREND_EMA_PERIOD, adjust=False).mean()
    ema200 = "BUY" if df["Close"].iloc[-1] > ema_series.iloc[-1] else "SELL"
    dist = abs(df["Close"].iloc[-1] - ema_series.iloc[-1])
    global_clear = len(df) >= TREND_EMA_PERIOD and dist >= GLOBAL_DIST_ATR * atr

    if structure is not None:
        if global_clear and structure != ema200:
            return None, "невизначеність: локальний супроти глобального", er
        return structure, "structure", er
    if global_clear:
        return ema200, "ema200", er
    return None, "немає чіткого тренда", er


# ---------------------------------------------------------------------------
# ЗОНИ (v3: діапазони, а не лінії)
# ---------------------------------------------------------------------------
def compute_zones(window: pd.DataFrame, low: float, high: float):
    """Наближення VRVP. Повертає список словників {lo, hi, touches, share}:
    кожна сира зона — це смуга [lo, hi], обсяг свічки розподіляється по
    смугах, які перекриває її High-Low."""
    if high <= low:
        mid = (high + low) / 2
        return [{"lo": mid, "hi": mid, "touches": 1, "share": 1.0}]
    zone_size = (high - low) / POC_ZONES
    touches = [0] * POC_ZONES
    vol = [0.0] * POC_ZONES
    has_vol = "Volume" in window.columns and window["Volume"].sum() > 0
    for _, row in window.iterrows():
        z_lo = max(0, min(POC_ZONES - 1, int((row["Low"] - low) / zone_size)))
        z_hi = max(0, min(POC_ZONES - 1, int((row["High"] - low) / zone_size)))
        span = z_hi - z_lo + 1
        v = (row["Volume"] / span) if has_vol else (1.0 / span)
        for z in range(z_lo, z_hi + 1):
            touches[z] += 1
            vol[z] += v
    total = sum(vol) or 1.0
    zones = []
    for i in range(POC_ZONES):
        if touches[i] > 0:
            zones.append({"lo": low + zone_size * i,
                          "hi": low + zone_size * (i + 1),
                          "touches": touches[i],
                          "share": vol[i] / total})
    return zones


def merge_zone_bands(zones):
    """Зливає сусідні смуги в одну ЗОНУ, якщо проміжок між ними малий
    (або вони суміжні) — як прямокутник на графіку трейдера."""
    if not zones:
        return []
    zs = sorted(zones, key=lambda z: z["lo"])
    merged = [dict(zs[0])]
    for z in zs[1:]:
        cur = merged[-1]
        if z["lo"] <= cur["hi"] * (1 + ZONE_MERGE_PCT):
            tot = cur["share"] + z["share"]
            cur["hi"] = z["hi"]
            cur["touches"] += z["touches"]
            cur["share"] = tot
        else:
            merged.append(dict(z))
    return merged


def build_entry_ladder(window: pd.DataFrame, low: float, high: float, trend: str):
    """Драбина входів із ЗОН: 1 = перша на шляху відкату, 2 = глибша,
    остання = POC (зона з макс. обсягом)."""
    zones = merge_zone_bands(compute_zones(window, low, high))
    zones = [z for z in zones if z["touches"] >= MIN_ZONE_TOUCHES]
    if not zones:
        return []
    by_strength = sorted(zones, key=lambda z: z["share"], reverse=True)
    poc = by_strength[0]
    poc_c = (poc["lo"] + poc["hi"]) / 2
    edge = high if trend == "BUY" else low
    others = by_strength[1:]
    if trend == "BUY":
        cands = [z for z in others if poc_c <= (z["lo"] + z["hi"]) / 2 <= edge]
        cands.sort(key=lambda z: -(z["lo"] + z["hi"]) / 2)
    else:
        cands = [z for z in others if edge <= (z["lo"] + z["hi"]) / 2 <= poc_c]
        cands.sort(key=lambda z: (z["lo"] + z["hi"]) / 2)

    ladder = []
    for idx, z in enumerate(cands[:2] + [poc], start=1):
        is_poc = z is poc
        ladder.append({
            "level": idx,
            "lo": z["lo"],
            "hi": z["hi"],
            "center": (z["lo"] + z["hi"]) / 2,
            "strength": z["share"],
            "is_poc": is_poc,
            "expiry_min": LEVEL_EXPIRY_POC_MIN if is_poc else LEVEL_EXPIRY_BASE_MIN,
        })
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
    """Ціна всередині зони (з малим буфером на межах)."""
    return zone["lo"] * (1 - TOLERANCE_PCT) <= price <= zone["hi"] * (1 + TOLERANCE_PCT)


def approach_ok(df5: pd.DataFrame, zone: dict, trend: str) -> bool:
    """Ціна має ЗАЙТИ в зону з боку відкату: BUY — зверху (нещодавні хай
    вищі за верх зони), SELL — знизу (нещодавні лоу нижчі за низ зони).
    Це і є робота зони як підтримки/опору залежно від боку підходу."""
    if len(df5) < 4:
        return False
    recent = df5.iloc[-4:-1]  # три останні закриті свічки
    if trend == "BUY":
        return recent["High"].max() >= zone["hi"]
    return recent["Low"].min() <= zone["lo"]


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
        level_label = "POC (макс. обсяг)"
    else:
        level_label = {1: "Зона 1 (1-й дотик)", 2: "Зона 2 (глибше)"}.get(
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

    ladder = build_entry_ladder(window, low, high, trend)
    if not ladder:
        print(f"{pair}: не вдалось побудувати зони")
        return None

    current_price = float(df5["Close"].iloc[-1])
    breakout = is_impulsive_breakout(df5, trend)

    if cooldown_active(state, pair):
        print(f"{pair}: кулдаун активний")
        return None

    candidate = None
    for level in ladder:
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
                     "score": score, "breakout": breakout, "signal_key": signal_key}
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
