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

    requests.post(url, data=payload, timeout=20)


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

    passed_52w = strength_52w >= MIN_52W_STRENGTH
    passed_ema = ema50 > ema150 > ema200
    passed_rs = stock_return_6m > ihsg_return_6m

    # Filter wajib baru:
    # 1. Close / High 52W >= 0.80
    # 2. EMA50 > EMA150 > EMA200
    # 3. Return 6 bulan > IHSG
    if passed_52w and passed_ema and passed_rs:
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
            "score": round(score, 1)
        }
        
    print(
        f"{ticker} CHECK | "
        f"52W: {passed_52w} ({strength_52w:.1%}) | "
        f"EMA: {passed_ema} | "
        f"RS: {passed_rs} "
        f"({stock_return_6m:.1%} vs IHSG {ihsg_return_6m:.1%}) | "
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
            f"Tanggal: {today}\n\n"
            f"Tidak ada saham yang lolos filter hari ini.\n\n"
            f"Kriteria Wajib:\n"
            f"✅ Close / High 52W ≥ 0.80\n"
            f"✅ EMA50 > EMA150 > EMA200\n"
            f"✅ Return 6 bulan > IHSG\n\n"
            f"Info Tambahan:\n"
            f"• Volume Ratio = Volume hari ini / Avg Volume 20D"
        )
        send_telegram(message)
        return

    message = (
        f"📈 <b>LIVERMORE SCREENER - IDX</b>\n"
        f"Tanggal: {today}\n\n"
        f"Kriteria Wajib:\n"
        f"✅ Close / High 52W ≥ 0.80\n"
        f"✅ EMA50 > EMA150 > EMA200\n"
        f"✅ Return 6 bulan > IHSG\n\n"
        f"Info Tambahan:\n"
        f"• Volume Ratio = Volume hari ini / Avg Volume 20D\n\n"
        f"🏆 <b>Top Candidates:</b>\n"
    )

    for i, r in enumerate(results[:20], start=1):
        volume_status = "🔥 Active" if r["volume_active"] else "Normal"

        message += (
            f"\n{i}. <b>{r['ticker']}</b> | Score: {r['score']}\n"
            f"Close: {r['close']:.0f}\n"
            f"52W Strength: {r['strength_52w']:.1%}\n"
            f"Return 6M: {r['return_6m']:.1%} vs IHSG {r['ihsg_return_6m']:.1%}\n"
            f"Volume: {r['volume_ratio']:.2f}x Avg 20D ({volume_status})\n"
        )

    message += (
        f"\nTotal lolos: {len(results)} saham\n\n"
        f"Catatan: ini bukan rekomendasi beli/jual. "
        f"Gunakan sebagai watchlist dan tunggu pivotal point/breakout."
    )

    send_telegram(message)


if __name__ == "__main__":
    main()
