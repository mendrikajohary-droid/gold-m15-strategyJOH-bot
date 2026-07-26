import os
import json
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from ta.trend import EMAIndicator, MACD, ADXIndicator
from ta.momentum import RSIIndicator
import yfinance as yf

# ============================================================
# CONFIGURATION — correspond exactement aux paramètres du script Pine
# ============================================================
TICKER = "GC=F"
FAST_LEN, SLOW_LEN = 9, 21
TREND_LEN = 200
USE_RSI_FILTER = True
RSI_LEN, RSI_LONG_MAX, RSI_SHORT_MIN = 14, 70, 30
USE_MACD_FILTER = True
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
USE_BB_FILTER = False          # désactivé, comme sur ta capture
USE_VOL_FILTER = True
VOL_LEN, VOL_MULT = 20, 1.2
USE_MTF_FILTER = True
MTF_TIMEFRAME = "1h"           # "Timeframe supérieur" = 1 heure
MTF_EMA_LEN = 50
USE_ADX_FILTER = True
ADX_LEN, ADX_THRESHOLD = 14, 20

SL_PERC, TP1_PERC, TP2_PERC, TP3_PERC = 1.0, 1.0, 2.0, 3.0
USE_TRAIL = True
TRAIL_PERC = 1.0
USE_BE = True
BE_BUFFER_PERC = 0.1
USE_DD_PROTECTION = True
MAX_DD_PERCENT = 10.0
INITIAL_CAPITAL = 10000.0
RISK_PERCENT = 1.0

MAX_PENDING_BARS = 5   # 5 x 15min = 75 min max d'attente d'une confirmation

STATE_FILE = "state.json"
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"Erreur envoi Telegram: {e}")

# ============================================================
# ÉTAT PERSISTANT
# ============================================================
def load_state():
    default = {
        "pending_long": False, "pending_short": False, "pending_start": None,
        "in_long": False, "in_short": False,
        "entry_price": None, "sl": None, "tp1": None, "tp2": None, "tp3": None,
        "tp1_hit": False, "trail_stop": None,
        "tp1_sent": False, "tp2_sent": False, "tp3_sent": False,
        "equity": INITIAL_CAPITAL, "equity_peak": INITIAL_CAPITAL
    }
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            saved = json.load(f)
        default.update(saved)
    return default

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ============================================================
# DONNÉES + INDICATEURS
# ============================================================
def fetch_15m():
    df = yf.download(TICKER, period="60d", interval="15m", progress=False, auto_adjust=True)
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df.dropna()

def fetch_mtf_trend():
    df = yf.download(TICKER, period="180d", interval=MTF_TIMEFRAME, progress=False, auto_adjust=True)
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.dropna()
    ema = EMAIndicator(df["Close"], window=MTF_EMA_LEN).ema_indicator()
    return df["Close"].iloc[-1], ema.iloc[-1]

def compute_indicators(df):
    close = df["Close"]
    ind = {}
    ind["ema_fast"] = EMAIndicator(close, window=FAST_LEN).ema_indicator()
    ind["ema_slow"] = EMAIndicator(close, window=SLOW_LEN).ema_indicator()
    ind["ema_trend"] = EMAIndicator(close, window=TREND_LEN).ema_indicator()
    ind["rsi"] = RSIIndicator(close, window=RSI_LEN).rsi()
    macd = MACD(close, window_slow=MACD_SLOW, window_fast=MACD_FAST, window_sign=MACD_SIGNAL)
    ind["macd_line"] = macd.macd()
    ind["macd_signal"] = macd.macd_signal()
    adx = ADXIndicator(df["High"], df["Low"], close, window=ADX_LEN)
    ind["adx"] = adx.adx()
    return ind

# ============================================================
# LOGIQUE PRINCIPALE
# ============================================================
def main():
    state = load_state()
    df = fetch_15m()
    ind = compute_indicators(df)
    mtf_close, mtf_ema = (None, None)
    if USE_MTF_FILTER:
        mtf_close, mtf_ema = fetch_mtf_trend()

    close = df["Close"].iloc[-1]
    price_time = df.index[-1]

    cross_up = ind["ema_fast"].iloc[-2] <= ind["ema_slow"].iloc[-2] and ind["ema_fast"].iloc[-1] > ind["ema_slow"].iloc[-1]
    cross_down = ind["ema_fast"].iloc[-2] >= ind["ema_slow"].iloc[-2] and ind["ema_fast"].iloc[-1] < ind["ema_slow"].iloc[-1]

    trend_ok_long = close > ind["ema_trend"].iloc[-1]
    trend_ok_short = close < ind["ema_trend"].iloc[-1]
    macd_ok_long = (not USE_MACD_FILTER) or ind["macd_line"].iloc[-1] > ind["macd_signal"].iloc[-1]
    macd_ok_short = (not USE_MACD_FILTER) or ind["macd_line"].iloc[-1] < ind["macd_signal"].iloc[-1]

    core_long_no_cross = trend_ok_long and macd_ok_long
    core_short_no_cross = trend_ok_short and macd_ok_short

    rsi_ok_long = (not USE_RSI_FILTER) or ind["rsi"].iloc[-1] < RSI_LONG_MAX
    rsi_ok_short = (not USE_RSI_FILTER) or ind["rsi"].iloc[-1] > RSI_SHORT_MIN
    mtf_ok_long = (not USE_MTF_FILTER) or mtf_close > mtf_ema
    mtf_ok_short = (not USE_MTF_FILTER) or mtf_close < mtf_ema
    adx_ok = (not USE_ADX_FILTER) or ind["adx"].iloc[-1] > ADX_THRESHOLD

    confirm_long = rsi_ok_long and mtf_ok_long and adx_ok
    confirm_short = rsi_ok_short and mtf_ok_short and adx_ok

    dd_ok = True
    if USE_DD_PROTECTION:
        state["equity_peak"] = max(state["equity_peak"], state["equity"])
        current_dd = (state["equity_peak"] - state["equity"]) / state["equity_peak"] * 100 if state["equity_peak"] > 0 else 0
        dd_ok = current_dd < MAX_DD_PERCENT

    core_long = cross_up and core_long_no_cross and dd_ok
    core_short = cross_down and core_short_no_cross and dd_ok

    if not state["in_long"] and not state["in_short"]:
        if state["pending_long"] and not core_long_no_cross:
            state["pending_long"] = False
        if state["pending_short"] and not core_short_no_cross:
            state["pending_short"] = False

        if state["pending_long"] and state["pending_start"]:
            elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(state["pending_start"])
            if elapsed > timedelta(minutes=MAX_PENDING_BARS * 15):
                state["pending_long"] = False
                send_telegram("⌛ Opportunité LONG expirée (Or M15) sans confirmation.")
        if state["pending_short"] and state["pending_start"]:
            elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(state["pending_start"])
            if elapsed > timedelta(minutes=MAX_PENDING_BARS * 15):
                state["pending_short"] = False
                send_telegram("⌛ Opportunité SHORT expirée (Or M15) sans confirmation.")

        if not state["pending_long"] and not state["pending_short"]:
            if core_long and not confirm_long:
                state["pending_long"] = True
                state["pending_start"] = datetime.now(timezone.utc).isoformat()
                missing = []
                if not rsi_ok_long: missing.append("RSI")
                if not mtf_ok_long: missing.append("MTF")
                if not adx_ok: missing.append("ADX")
                send_telegram(f"🟡 OPPORTUNITÉ LONG (Or M15)\nPrix: {close:.2f}\nManque: {', '.join(missing)}\n⏳ En attente de confirmation...")
            elif core_short and not confirm_short:
                state["pending_short"] = True
                state["pending_start"] = datetime.now(timezone.utc).isoformat()
                missing = []
                if not rsi_ok_short: missing.append("RSI")
                if not mtf_ok_short: missing.append("MTF")
                if not adx_ok: missing.append("ADX")
                send_telegram(f"🟡 OPPORTUNITÉ SHORT (Or M15)\nPrix: {close:.2f}\nManque: {', '.join(missing)}\n⏳ En attente de confirmation...")

        entry_long = (core_long and confirm_long) or (state["pending_long"] and core_long_no_cross and confirm_long)
        entry_short = (core_short and confirm_short) or (state["pending_short"] and core_short_no_cross and confirm_short)

        if entry_long:
            state.update({
                "in_long": True, "pending_long": False,
                "entry_price": close, "sl": close * (1 - SL_PERC / 100),
                "tp1": close * (1 + TP1_PERC / 100), "tp2": close * (1 + TP2_PERC / 100), "tp3": close * (1 + TP3_PERC / 100),
                "tp1_hit": False, "trail_stop": None, "tp1_sent": False, "tp2_sent": False, "tp3_sent": False
            })
            send_telegram(f"🟢 ENTRÉE LONG CONFIRMÉE (Or M15)\nPrix: {close:.2f}\nSL: {state['sl']:.2f}\nTP1: {state['tp1']:.2f}\nTP2: {state['tp2']:.2f}\nTP3: {state['tp3']:.2f}")

        elif entry_short:
            state.update({
                "in_short": True, "pending_short": False,
                "entry_price": close, "sl": close * (1 + SL_PERC / 100),
                "tp1": close * (1 - TP1_PERC / 100), "tp2": close * (1 - TP2_PERC / 100), "tp3": close * (1 - TP3_PERC / 100),
                "tp1_hit": False, "trail_stop": None, "tp1_sent": False, "tp2_sent": False, "tp3_sent": False
            })
            send_telegram(f"🔴 ENTRÉE SHORT CONFIRMÉE (Or M15)\nPrix: {close:.2f}\nSL: {state['sl']:.2f}\nTP1: {state['tp1']:.2f}\nTP2: {state['tp2']:.2f}\nTP3: {state['tp3']:.2f}")

    elif state["in_long"]:
        if not state["tp1_hit"] and close >= state["tp1"]:
            state["tp1_hit"] = True
            state["trail_stop"] = close * (1 - TRAIL_PERC / 100)
        if state["tp1_hit"] and USE_TRAIL:
            new_trail = close * (1 - TRAIL_PERC / 100)
            state["trail_stop"] = max(state["trail_stop"], new_trail)
            be_level = state["entry_price"] * (1 + BE_BUFFER_PERC / 100)
            active_stop = max(state["trail_stop"], be_level) if USE_BE else state["trail_stop"]
        else:
            active_stop = state["sl"]

        if not state["tp1_sent"] and close >= state["tp1"]:
            state["tp1_sent"] = True
            send_telegram(f"✅ TP1 atteint (Or M15 LONG) à {close:.2f}")
        if not state["tp2_sent"] and close >= state["tp2"]:
            state["tp2_sent"] = True
            send_telegram(f"✅ TP2 atteint (Or M15 LONG) à {close:.2f}")
        if close >= state["tp3"]:
            gain = (state["tp3"] - state["entry_price"]) / state["entry_price"] * state["equity"] * RISK_PERCENT / 100 * 3
            state["equity"] += abs(gain)
            send_telegram(f"🏁 TP3 atteint (Or M15 LONG) à {close:.2f} — position fermée")
            state.update({"in_long": False, "entry_price": None, "sl": None, "tp1": None, "tp2": None, "tp3": None, "tp1_hit": False, "trail_stop": None})
        elif close <= active_stop:
            reason = "trailing stop / break-even" if state["tp1_hit"] else "stop loss"
            loss = state["equity"] * RISK_PERCENT / 100 if not state["tp1_hit"] else 0
            state["equity"] -= loss
            send_telegram(f"⛔ Position LONG (Or M15) fermée à {close:.2f} ({reason})")
            state.update({"in_long": False, "entry_price": None, "sl": None, "tp1": None, "tp2": None, "tp3": None, "tp1_hit": False, "trail_stop": None})

    elif state["in_short"]:
        if not state["tp1_hit"] and close <= state["tp1"]:
            state["tp1_hit"] = True
            state["trail_stop"] = close * (1 + TRAIL_PERC / 100)
        if state["tp1_hit"] and USE_TRAIL:
            new_trail = close * (1 + TRAIL_PERC / 100)
            state["trail_stop"] = min(state["trail_stop"], new_trail)
            be_level = state["entry_price"] * (1 - BE_BUFFER_PERC / 100)
            active_stop = min(state["trail_stop"], be_level) if USE_BE else state["trail_stop"]
        else:
            active_stop = state["sl"]

        if not state["tp1_sent"] and close <= state["tp1"]:
            state["tp1_sent"] = True
            send_telegram(f"✅ TP1 atteint (Or M15 SHORT) à {close:.2f}")
        if not state["tp2_sent"] and close <= state["tp2"]:
            state["tp2_sent"] = True
            send_telegram(f"✅ TP2 atteint (Or M15 SHORT) à {close:.2f}")
        if close <= state["tp3"]:
            state["equity"] += state["equity"] * RISK_PERCENT / 100 * 3
            send_telegram(f"🏁 TP3 atteint (Or M15 SHORT) à {close:.2f} — position fermée")
            state.update({"in_short": False, "entry_price": None, "sl": None, "tp1": None, "tp2": None, "tp3": None, "tp1_hit": False, "trail_stop": None})
        elif close >= active_stop:
            reason = "trailing stop / break-even" if state["tp1_hit"] else "stop loss"
            loss = state["equity"] * RISK_PERCENT / 100 if not state["tp1_hit"] else 0
            state["equity"] -= loss
            send_telegram(f"⛔ Position SHORT (Or M15) fermée à {close:.2f} ({reason})")
            state.update({"in_short": False, "entry_price": None, "sl": None, "tp1": None, "tp2": None, "tp3": None, "tp1_hit": False, "trail_stop": None})

    save_state(state)
    print(f"[{price_time}] Vérification terminée. Prix: {close:.2f}")

if __name__ == "__main__":
    main()
