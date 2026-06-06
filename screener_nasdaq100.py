import os
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime

# =========================
# CONFIG
# =========================

BENCHMARK_TICKER = "QQQ"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MIN_52W_STRENGTH = 0.85


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

def load_nasdaq100_tickers():
    url = "https://en.wikipedia.org/wiki/Nasdaq-100"

    tables = pd.read_html(url)

    tickers = []

    for table in tables:
        if "Ticker" in table.columns:
            tickers = table["Ticker"].tolist()
            break
        elif "Symbol" in table.columns:
            tickers = table["Symbol"].tolist()
            break

    tickers = [str(t).strip().replace(".", "-") for t in tickers if str(t).strip()]

    print(f"Total Nasdaq 100 tickers loaded: {len(tickers)}")
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

def analyze_stock(ticker, benchmark_return_6m):
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
    passed_rs = stock_return_6m > benchmark_return_6m

    if passed_52w and passed_ema and passed_rs:
        score = (
            min(strength_52w / 1.0, 1) * 40 +
            35 +
            min((stock_return_6m - benchmark_return_6m) / 0.30, 1) * 15 +
            min(volume_ratio / 2, 1) * 10
        )

        return {
            "ticker": ticker,
            "close": close,
            "high_52w": high_52w,
            "strength_52w": strength_52w,
            "return_6m": stock_return_6m,
            "benchmark_return_6m": benchmark_return_6m,
            "volume_ratio": volume_ratio,
            "volume_active": volume_active,
            "score": round(score, 1)
        }

    print(
        f"{ticker} CHECK | "
        f"52W: {passed_52w} ({strength_52w:.1%}) | "
        f"EMA: {passed_ema} | "
        f"RS: {passed_rs} "
        f"({stock_return_6m:.1%} vs QQQ {benchmark_return_6m:.1%}) | "
        f"VOL: {volume_ratio:.2f}x"
    )

    return None


# =========================
# MAIN
# =========================

def main():
    print("Running Livermore Screener - Nasdaq 100...")

    tickers = load_nasdaq100_tickers()

    benchmark_df = get_data(BENCHMARK_TICKER)
    if benchmark_df is None:
        print("Gagal mengambil data QQQ.")
        send_telegram("Gagal mengambil data QQQ dari yfinance.")
        return

    benchmark_return_6m = calculate_return_6m(benchmark_df)
    print(f"QQQ return 6M: {benchmark_return_6m:.2%}")

    results = []

    for ticker in tickers:
        print(f"Processing {ticker}...")
        try:
            result = analyze_stock(ticker, benchmark_return_6m)
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
            f"📈 <b>LIVERMORE SCREENER - NASDAQ100</b>\n"
            f"{today}\n\n"
            f"Tidak ada saham yang lolos.\n\n"
            f"Filter: 52W ≥85% | EMA50>150>200 | RS>QQQ"
        )
        send_telegram(message)
        return

    message = (
        f"📈 <b>LIVERMORE SCREENER - NASDAQ100</b>\n"
        f"{today}\n"
        f"QQQ 6M: {benchmark_return_6m:.1%}\n\n"
        f"<b>Ticker | Score | 52W | RS vs QQQ | Vol</b>\n"
    )

    for i, r in enumerate(results[:15], start=1):
        rs_vs_benchmark = r["return_6m"] - r["benchmark_return_6m"]
        volume_icon = "🔥" if r["volume_active"] else ""

        message += (
            f"{i}. <b>{r['ticker']}</b> | "
            f"{r['score']:.0f} | "
            f"{r['strength_52w']:.0%} | "
            f"{rs_vs_benchmark:+.1%} | "
            f"{r['volume_ratio']:.1f}x {volume_icon}\n"
        )

    message += (
        f"\nTotal: {len(results)} saham\n"
        f"Filter: 52W≥85%, EMA trend, RS>QQQ"
    )

    send_telegram(message)


if __name__ == "__main__":
    main()
