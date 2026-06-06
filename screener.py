import os
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime

# =========================
# CONFIG
# =========================

TICKER_FILE = "tickers_idx.txt"
IHSG_TICKER = "^JKSE"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MIN_52W_STRENGTH = 0.80
MIN_AVG_VALUE_20D = 10_000_000_000  # Rp10 miliar


# =========================
# TELEGRAM
# =========================

def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram token/chat_id belum diset.")
        print(message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    response = requests.post(url, data=payload, timeout=20)

    print(f"Telegram status: {response.status_code}")
    print(response.text)


# =========================
# DATA HELPER
# =========================

def load_tickers():
    with open(TICKER_FILE, "r") as f:
        tickers = [line.strip() for line in f if line.strip()]
    return tickers


def get_data(ticker):
    df = yf.download(
        ticker,
        period="2y",
        interval="1d",
        auto_adjust=True,
        progress=False
    )

    if df.empty or len(df) < 220:
        return None

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    return df


def calculate_return_6m(df):
    close_now = df["Close"].iloc[-1]
    close_6m = df["Close"].iloc[-126]
    return (close_now / close_6m) - 1


# =========================
# SCREENER LOGIC
# =========================

def analyze_stock(ticker, ihsg_return_6m):
    df = get_data(ticker)

    if df is None:
        return None

    close = df["Close"].iloc[-1]
    volume_today = df["Volume"].iloc[-1]

    high_52w = df["High"].tail(252).max()
    strength_52w = close / high_52w

    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()
    df["EMA150"] = df["Close"].ewm(span=150, adjust=False).mean()
    df["EMA200"] = df["Close"].ewm(span=200, adjust=False).mean()

    ema50 = df["EMA50"].iloc[-1]
    ema150 = df["EMA150"].iloc[-1]
    ema200 = df["EMA200"].iloc[-1]

    stock_return_6m = calculate_return_6m(df)

    avg_volume_20d = df["Volume"].rolling(20).mean().iloc[-1]
    volume_ratio = volume_today / avg_volume_20d if avg_volume_20d > 0 else 0
    volume_active = volume_today > avg_volume_20d

    value_today = close * volume_today
    avg_value_20d = (df["Close"] * df["Volume"]).rolling(20).mean().iloc[-1]

    passed_52w = strength_52w >= MIN_52W_STRENGTH
    passed_ema = ema50 > ema150 > ema200
    passed_rs = stock_return_6m > ihsg_return_6m
    passed_liquidity = avg_value_20d >= MIN_AVG_VALUE_20D

    if passed_52w and passed_ema and passed_rs and passed_liquidity:
        score = (
            min(strength_52w / 1.0, 1) * 35 +
            35 +
            min((stock_return_6m - ihsg_return_6m) / 0.30, 1) * 20 +
            min(volume_ratio / 2, 1) * 10
        )

        return {
            "ticker": ticker,
            "close": close,
            "high_52w": high_52w,
            "strength_52w": strength_52w,
            "return_6m": stock_return_6m,
            "ihsg_return_6m": ihsg_return_6m,
            "volume_ratio": volume_ratio,
            "volume_active": volume_active,
            "value_today": value_today,
            "avg_value_20d": avg_value_20d,
            "score": round(score, 1)
        }

    print(
        f"{ticker} CHECK | "
        f"52W: {passed_52w} ({strength_52w:.1%}) | "
        f"EMA: {passed_ema} | "
        f"RS: {passed_rs} "
        f"({stock_return_6m:.1%} vs IHSG {ihsg_return_6m:.1%}) | "
        f"LIQ: {passed_liquidity} "
        f"(Avg Rp{avg_value_20d / 1_000_000_000:.1f}B) | "
        f"VOL: {volume_ratio:.2f}x"
    )

    return None


# =========================
# MAIN
# =========================

def main():
    print("Running Livermore Screener...")

    tickers = load_tickers()
    print(f"Total tickers loaded: {len(tickers)}")

    ihsg_df = get_data(IHSG_TICKER)
    if ihsg_df is None:
        print("Gagal mengambil data IHSG.")
        send_telegram("Gagal mengambil data IHSG dari yfinance.")
        return

    ihsg_return_6m = calculate_return_6m(ihsg_df)
    print(f"IHSG return 6M: {ihsg_return_6m:.2%}")

    results = []

    for ticker in tickers:
        print(f"Processing {ticker}...")
        try:
            result = analyze_stock(ticker, ihsg_return_6m)
            if result:
                print(f"PASSED: {ticker}")
                results.append(result)
            else:
                print(f"FAILED: {ticker}")
        except Exception as e:
            print(f"ERROR {ticker}: {e}")

    print(f"Total passed: {len(results)}")

    results = sorted(results, key=lambda x: x["score"], reverse=True)

    today = datetime.now().strftime("%d %b %Y")

    if not results:
        message = (
            f"📈 <b>LIVERMORE SCREENER - IDX</b>\n"
            f"{today}\n\n"
            f"Tidak ada saham yang lolos.\n\n"
            f"Filter: 52W ≥80% | EMA50>150>200 | RS>IHSG | Avg Value20D>Rp10B"
        )
        send_telegram(message)
        return

    message = (
        f"📈 <b>LIVERMORE SCREENER - IDX</b>\n"
        f"{today}\n"
        f"IHSG 6M: {ihsg_return_6m:.1%}\n\n"
        f"<b>Ticker | Score | 52W | RS vs IHSG | Vol</b>\n"
    )

    for i, r in enumerate(results[:15], start=1):
        ticker_clean = r["ticker"].replace(".JK", "")
        rs_vs_ihsg = r["return_6m"] - r["ihsg_return_6m"]
        volume_icon = "🔥" if r["volume_active"] else ""

        message += (
            f"{i}. <b>{ticker_clean}</b> | "
            f"{r['score']:.0f} | "
            f"{r['strength_52w']:.0%} | "
            f"{rs_vs_ihsg:+.1%} | "
            f"{r['volume_ratio']:.1f}x {volume_icon}\n"
        )

    message += (
        f"\nTotal: {len(results)} saham\n"
        f"Filter: 52W≥80%, EMA trend, RS>IHSG, Avg Value20D>Rp10B"
    )

    send_telegram(message)


if __name__ == "__main__":
    main()
