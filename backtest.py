import csv, os, time
from datetime import datetime
import pandas as pd
import yfinance as yf
from forex_bot import *

BACKTEST_DAYS = 60
OUTPUT_FILE = "/data/data/com.termux/files/home/forex-signal-bot/backtest_results.csv"
M5_SLICE = 300
M30_SLICE = 250
FIELDS = ["pair", "ts", "direction", "level", "zone_lo", "zone_hi", "entry_price", "strength_pct", "strength_bucket", "er", "atr_ratio", "p5m", "p10m", "p15m", "res5m", "res10m", "res15m"]

def fetch_pair_data(pair):
    try:
        df5 = yf.download(pair, interval="5m", period=f"{BACKTEST_DAYS}d", progress=False, auto_adjust=False)
        df30 = yf.download(pair, interval="30m", period=f"{BACKTEST_DAYS}d", progress=False, auto_adjust=False)
        if df5.empty or df30.empty: return None, None
        if isinstance(df5.columns, pd.MultiIndex): df5.columns = df5.columns.get_level_values(0)
        if isinstance(df30.columns, pd.MultiIndex): df30.columns = df30.columns.get_level_values(0)
        df5, df30 = df5.dropna(), df30.dropna()
        if df5.index.tz is not None: df5.index = df5.index.tz_convert("UTC").tz_localize(None)
        if df30.index.tz is not None: df30.index = df30.index.tz_convert("UTC").tz_localize(None)
        return df5, df30
    except Exception: return None, None

def backtest_pair(pair):
    print(f"\n=== {pair} ===")
    df5, df30 = fetch_pair_data(pair)
    if df5 is None:
        print("  немає даних")
        return []
    
    signals = []
    start_idx = max(IMPULSE_LOOKBACK_5M, PROFILE_LOOKBACK_5M) + 50
    end_idx = len(df5) - 5
    if end_idx <= start_idx: return []

    total = end_idx - start_idx
    last_signal_ts = None
    current_sig = None
    current_ladder = None
    next_progress = 0.10

    for i in range(start_idx, end_idx):
        progress = (i - start_idx) / total
        if progress >= next_progress:
            print(f"  {progress*100:.0f}% | сигналів: {len(signals)}")
            next_progress += 0.10

        ts = df5.index[i]
        if last_signal_ts and (ts - last_signal_ts).total_seconds() / 60 < SIGNAL_COOLDOWN_MIN:
            continue

        pos = df30.index.searchsorted(ts, side="right")
        if pos < CONSOLIDATION_LOOKBACK + 1: continue
        df5_hist = df5.iloc[max(0, i - M5_SLICE):i + 1]
        df30_hist = df30.iloc[max(0, pos - M30_SLICE):pos]

        trend, source, er = analyze_trend_30m(df30_hist)
        if trend is None: continue

        low, high, _ = find_last_impulse(df5_hist)
        impulse_range = high - low
        if impulse_range <= 0: continue
        current_price = float(df5_hist["Close"].iloc[-1])
        
        pullback = (high - current_price) / impulse_range if trend == "BUY" else (current_price - low) / impulse_range
        if pullback < MIN_PULLBACK_FRACTION: continue

        sig = f"{low:.5f}|{high:.5f}"
        
        if sig != current_sig:
            profile_window = df5_hist.tail(PROFILE_LOOKBACK_5M)
            new_ladder = build_entry_ladder(profile_window, trend)
            if new_ladder:
                current_sig = sig
                current_ladder = new_ladder
            else:
                current_sig = sig
                current_ladder = None

        if not current_ladder: continue
        
        poc_zone = next(l for l in current_ladder if l["is_poc"])
        if zone_broken(poc_zone, df5_hist, trend):
            current_ladder = None
            continue
            
        breakout = is_impulsive_breakout(df5_hist, trend)
        atr_ratio = compute_atr_ratio(df5_hist, 14, 20)
        
        for level in current_ladder:
            if zone_broken(level, df5_hist, trend): continue
            if zone_is_dwelling(df5_hist, level): continue
            if not in_zone(current_price, level, trend): continue
            if not approach_ok(df5_hist, level, trend): continue
            
            width = max(level["hi"] - level["lo"], 1e-12)
            depth = (level["hi"] - current_price) / width if trend == "BUY" else (current_price - level["lo"]) / width
            score = round(300 * float(level["strength"]) + 20 * (1.0 - min(1.0, max(0.0, depth))) + (15 if level["is_poc"] else 0) + 25 * min(1.0, er / 0.5), 1)
            
            if score < MIN_SCORE: continue

            res = {"pair": pair.replace("=X", ""), "ts": ts.strftime("%Y-%m-%d %H:%M"), "direction": trend,
                   "level": level["level"], "zone_lo": round(level["lo"], 5), "zone_hi": round(level["hi"], 5),
                   "entry_price": round(current_price, 5), "strength_pct": round(float(level["strength"]) * 100, 2),
                   "strength_bucket": classify_strength(float(level["strength"])), "er": round(er, 3), "atr_ratio": round(atr_ratio, 3)}
            
            for bars, label in [(1, "5m"), (2, "10m"), (3, "15m")]:
                if i + bars >= len(df5):
                    res[f"p{label}"], res[f"res{label}"] = "", "UNKNOWN"
                else:
                    ep = float(df5["Close"].iloc[i + bars])
                    res[f"p{label}"] = round(ep, 5)
                    res[f"res{label}"] = "WIN" if (ep > current_price if trend == "BUY" else ep < current_price) else "LOSS"
            
            signals.append(res)
            last_signal_ts = ts
            print(f"  >>> {res['ts']} {trend} lvl{level['level']} str={res['strength_pct']}% 5m={res['res5m']} 15m={res['res15m']}")
            break

    print(f"  Готово | сигналів: {len(signals)}")
    return signals

def main():
    all_signals = []
    for idx, pair in enumerate(PAIRS):
        print(f"\n[{idx+1}/{len(PAIRS)}]")
        try:
            all_signals.extend(backtest_pair(pair))
        except Exception as e:
            print(f"  ПОМИЛКА: {e}")
        if (idx + 1) % 3 == 0:
            with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                w.writeheader()
                for s in all_signals: w.writerow({k: s.get(k, "") for k in FIELDS})
            print(f">>> ПРОМІЖНО збережено: {len(all_signals)}")
        time.sleep(5)

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for s in all_signals: w.writerow({k: s.get(k, "") for k in FIELDS})
    print(f"ГОТОВО: {len(all_signals)}")

if __name__ == "__main__":
    main()
