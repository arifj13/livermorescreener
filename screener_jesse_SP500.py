"""
S&P 500 Momentum Screener (ala Livermore / academic momentum factor)
======================================================================
Metodologi:
- Universe   : S&P 500 (diambil otomatis, tidak di-hardcode)
- Momentum   : 12 bulan, exclude 1 bulan terakhir (12-1), standar Jegadeesh & Titman (1993)
- Filter     : 52-week high proximity >= 80% (George & Hwang, 2004)
- Filter     : Relative strength (momentum 12-1 saham > momentum 12-1 SPY)
- Scoring    : Risk-adjusted momentum = momentum_12_1 / volatilitas harian (Barroso & Santa-Clara, 2015)
- Output     : Top 20 saham, dikirim ke Telegram

Tidak memakai sector cap & market regime filter (disederhanakan sesuai kebutuhan).
"""

import os
import time
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime

# =========================
# CONFIG
# =========================

BENCHMARK_TICKER = "RSP"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MIN_52W_STRENGTH = 0.80     # min 80% dari 52-week high
MOMENTUM_LOOKBACK = 252     # ~12 bulan trading days
MOMENTUM_SKIP = 21          # exclude ~1 bulan terakhir
MIN_HISTORY_DAYS = MOMENTUM_LOOKBACK + MOMENTUM_SKIP + 5  # buffer aman

TOP_N = 20
BATCH_SIZE = 50             # jumlah ticker per batch download
BATCH_DELAY = 2             # jeda antar batch (detik), hindari rate limit

SP500_SOURCES = [
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
    "https://raw.githubusercontent.com/Ate329/top-us-stock-tickers/main/tickers/sp500.csv",
]


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

    try:
        response = requests.post(url, data=payload, timeout=20)
        print(f"Telegram status: {response.status_code}")
        if response.status_code != 200:
            print(response.text)
    except Exception as e:
        print(f"Gagal kirim Telegram: {e}")


# =========================
# UNIVERSE (S&P 500)
# =========================

def load_sp500_tickers():
    for url in SP500_SOURCES:
        try:
            df = pd.read_csv(url)
            col = "Symbol" if "Symbol" in df.columns else df.columns[0]

            tickers = (
                df[col]
                .astype(str)
                .str.strip()
                .str.replace(".", "-", regex=False)  # BRK.B -> BRK-B (format yfinance)
                .tolist()
            )
            tickers = [t for t in tickers if t and t.upper() == t and len(t) <= 6]

            if len(tickers) >= 400:  # sanity check, S&P 500 harusnya ~500
                print(f"Berhasil load {len(tickers)} ticker dari {url}")
                return sorted(set(tickers))
            else:
                print(f"Data dari {url} terlihat tidak lengkap ({len(tickers)} ticker), coba source lain...")

        except Exception as e:
            print(f"Gagal load dari {url}: {e}")

    print("Semua source gagal. Tidak bisa lanjut.")
    return []


# =========================
# DATA FETCH (BATCHED)
# =========================

def download_batch(tickers, batch_size=BATCH_SIZE, delay=BATCH_DELAY):
    all_data = {}

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(tickers) - 1) // batch_size + 1
        print(f"Downloading batch {batch_num}/{total_batches} ({len(batch)} ticker)...")

        try:
            df = yf.download(
                batch,
                period="2y",
                interval="1d",
                auto_adjust=True,
                group_by="ticker",
                threads=True,
                progress=False,
            )

            for t in batch:
                try:
                    sub = df if len(batch) == 1 else df[t]
                    sub = sub.dropna(subset=["Close"])  # buang baris hari ini yang belum ada datanya
                    if not sub.empty and len(sub) >= MIN_HISTORY_DAYS:
                        all_data[t] = sub
                except Exception:
                    continue

        except Exception as e:
            print(f"Batch {batch_num} error: {e}")

        time.sleep(delay)

    print(f"Total ticker dengan data valid: {len(all_data)}/{len(tickers)}")
    return all_data


def download_single(ticker):
    df = yf.download(ticker, period="2y", interval="1d", auto_adjust=True, progress=False)
    if df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Close"])  # buang baris hari ini yang belum ada datanya
    if len(df) < MIN_HISTORY_DAYS:
        return None
    return df


# =========================
# METRICS
# =========================

def calculate_momentum_12_1(df):
    """Return 12 bulan, exclude 1 bulan terakhir (Jegadeesh & Titman 1993)."""
    if len(df) < MIN_HISTORY_DAYS:
        return None
    price_t21 = df["Close"].iloc[-MOMENTUM_SKIP]
    price_t252 = df["Close"].iloc[-MOMENTUM_LOOKBACK]
    return (price_t21 / price_t252) - 1


def calculate_daily_volatility(df, window=MOMENTUM_LOOKBACK):
    """Standar deviasi return harian, dipakai untuk risk-adjust skor momentum."""
    returns = df["Close"].pct_change().dropna()
    return returns.tail(window).std()

def calculate_52w_strength(df):
    high_52w = df["Close"].tail(252).max()  # pakai Close, bukan High — hindari kolom High yang rawan corrupt saat batch download
    close_now = df["Close"].iloc[-1]
    return close_now / high_52w, high_52w, close_now
    
#def calculate_52w_strength(df):
#   high_52w = df["High"].tail(252).max()
#   close_now = df["Close"].iloc[-1]
#   return close_now / high_52w, high_52w, close_now


# =========================
# SCREENER LOGIC
# =========================

def analyze_stock(ticker, df, benchmark_momentum):
    try:
        momentum = calculate_momentum_12_1(df)
        if momentum is None:
            return None

        volatility = calculate_daily_volatility(df)
        if volatility is None or volatility == 0:
            return None

        strength_52w, high_52w, close_now = calculate_52w_strength(df)

        volume_today = df["Volume"].iloc[-1]
        avg_volume_20d = df["Volume"].rolling(20).mean().iloc[-1]
        volume_ratio = volume_today / avg_volume_20d if avg_volume_20d > 0 else 0

        passed_52w = strength_52w >= MIN_52W_STRENGTH
        passed_rs = momentum > benchmark_momentum

        if not (passed_52w and passed_rs):
            return None

        risk_adj_score = momentum / volatility

        return {
            "ticker": ticker,
            "close": close_now,
            "high_52w": high_52w,
            "strength_52w": strength_52w,
            "momentum_12_1": momentum,
            "volatility_annualized": volatility * (252 ** 0.5),
            "rs_vs_benchmark": momentum - benchmark_momentum,
            "volume_ratio": volume_ratio,
            "score": risk_adj_score,
        }

    except Exception as e:
        print(f"Error analyze {ticker}: {e}")
        return None


# =========================
# MAIN
# =========================

def main():
    print("=" * 50)
    print("Running Momentum Screener - S&P 500")
    print("=" * 50)

    tickers = load_sp500_tickers()
    if not tickers:
        send_telegram("⚠️ Screener gagal: tidak bisa load daftar S&P 500.")
        return

    # Benchmark (SPY)
    benchmark_df = download_single(BENCHMARK_TICKER)
    if benchmark_df is None:
        send_telegram("⚠️ Screener gagal: tidak bisa ambil data SPY.")
        return

    benchmark_momentum = calculate_momentum_12_1(benchmark_df)
    if benchmark_momentum is None:
        send_telegram("⚠️ Screener gagal: data SPY tidak cukup panjang.")
        return

    print(f"SPY momentum 12-1: {benchmark_momentum:.2%}")

    # Data saham (batched)
    stock_data = download_batch(tickers)
    
    # --- DEBUG: cek funnel filter ---
    count_52w = sum(1 for t, df in stock_data.items() 
                    if calculate_52w_strength(df)[0] >= MIN_52W_STRENGTH)
    count_rs = sum(1 for t, df in stock_data.items() 
                   if (calculate_momentum_12_1(df) or -999) > benchmark_momentum)
    print(f"Lolos 52W filter saja: {count_52w}")
    print(f"Lolos RS filter saja: {count_rs}")
    print(f"SPY momentum 12-1: {benchmark_momentum:.2%}")
    # --- END DEBUG ---

    # --- DEBUG TAMBAHAN: cek raw value kolom High ---
    sample_tickers = ["AAPL", "MSFT", "NVDA"]
    for t in sample_tickers:
        if t in stock_data:
            df_sample = stock_data[t]
            print(f"{t} | High NaN count: {df_sample['High'].isna().sum()}/{len(df_sample)} | "
                  f"High max 252d: {df_sample['High'].tail(252).max()} | "
                  f"Close now: {df_sample['Close'].iloc[-1]}")
    # --- END DEBUG TAMBAHAN ---
    
    results = []
    for ticker, df in stock_data.items():
        result = analyze_stock(ticker, df, benchmark_momentum)
        if result:
            results.append(result)

    print(f"Total lolos filter: {len(results)}")

    results = sorted(results, key=lambda x: x["score"], reverse=True)
    top_results = results[:TOP_N]

    today = datetime.now().strftime("%d %b %Y")

    if not top_results:
        message = (
            f"📈 <b>MOMENTUM SCREENER - S&amp;P 500</b>\n"
            f"{today}\n\n"
            f"Tidak ada saham yang lolos filter.\n\n"
            f"Filter: 52W≥{MIN_52W_STRENGTH:.0%} | Momentum 12-1 > SPY ({benchmark_momentum:.1%})"
        )
        send_telegram(message)
        return

    message = (
        f"📈 <b>MOMENTUM SCREENER - S&amp;P 500</b>\n"
        f"{today}\n"
        f"SPY Momentum 12-1: {benchmark_momentum:+.1%}\n"
        f"Total lolos filter: {len(results)}\n\n"
        f"<b>#  Ticker  Score  52W   Mom12-1  vs SPY</b>\n"
    )

    for i, r in enumerate(top_results, start=1):
        volume_icon = "🔥" if r["volume_ratio"] > 1.5 else ""
        message += (
            f"{i}. <b>{r['ticker']}</b> | "
            f"{r['score']:.1f} | "
            f"{r['strength_52w']:.0%} | "
            f"{r['momentum_12_1']:+.1%} | "
            f"{r['rs_vs_benchmark']:+.1%} {volume_icon}\n"
        )

    message += (
        f"\nScore = Momentum(12-1) / Volatilitas harian\n"
        f"Filter: 52W≥{MIN_52W_STRENGTH:.0%}, Momentum 12-1 &gt; SPY"
    )

    send_telegram(message)
    print("Selesai.")


if __name__ == "__main__":
    main()
