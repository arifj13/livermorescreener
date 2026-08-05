"""
Nasdaq 100 Momentum Screener (ala Livermore / academic momentum factor)
======================================================================
Versi ini disamakan sepenuhnya dengan metodologi screener S&P 500:

- Universe   : Nasdaq 100 (diambil otomatis, tidak di-hardcode)
- Benchmark  : QQQ (Nasdaq-100 EQUAL WEIGHT) -- bukan QQQ (cap-weighted), supaya
               konsisten dengan alasan yang sama seperti pemilihan RSP di screener
               S&P 500: menghindari bar relative-strength yang bias ke mega-cap.
- Momentum   : 12 bulan, exclude 1 bulan terakhir (12-1), standar Jegadeesh & Titman (1993)
- Filter     : 52-week high proximity >= 80% (George & Hwang, 2004), berbasis closing price
- Filter     : Relative strength (momentum 12-1 saham > momentum 12-1 QQQ)
- Scoring    : Risk-adjusted momentum = momentum_12_1 / volatilitas harian (Barroso & Santa-Clara, 2015)
- Dedup      : Kelas saham ganda (mis. GOOGL/GOOG) disatukan, ambil skor tertinggi
- Breakout   : Flag tambahan -> harga close hari ini bikin high baru N-hari + volume terkonfirmasi
- Pullback   : Jalur screening TERPISAH -> saham pemenang (momentum > benchmark), sedang
               koreksi ke rentang 52W 50%-95% dari puncak, tren jangka panjang masih utuh
               (>MA200), dan dekat support MA20/50/100 (heuristik technical analysis, bukan
               faktor akademis formal seperti komponen momentum utama -> WAJIB cek manual
               chart tiap kandidat sebelum entry, daftar ini watchlist, bukan sinyal siap eksekusi)
- Output     : Top 20 momentum + breakout flag + top 10 pullback setup, dikirim ke Telegram

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

BENCHMARK_TICKER = "QQQ"   # Nasdaq-100 equal weight, hindari bias mega-cap dari QQQ cap-weighted

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MIN_52W_STRENGTH = 0.80     # min 80% dari 52-week high (berbasis closing price)
MOMENTUM_LOOKBACK = 252     # ~12 bulan trading days
MOMENTUM_SKIP = 21          # exclude ~1 bulan terakhir
MIN_HISTORY_DAYS = MOMENTUM_LOOKBACK + MOMENTUM_SKIP + 5  # buffer aman

BREAKOUT_LOOKBACK = 20          # ~1 bulan trading days, jendela pembanding "high baru"
BREAKOUT_VOLUME_THRESHOLD = 1.5 # volume hari ini vs rata-rata 20 hari

# --- Config khusus pullback setup ---
# Catatan: PULLBACK_MIN_52W bukan Fibonacci retracement sesungguhnya (yang dihitung dari
# swing low ke swing high spesifik) -- ini proxy sederhana (close/52w_high). Floor 50%
# artinya bisa menangkap saham yang sudah turun s/d 50% dari puncaknya -- jaring lebih
# lebar, cek manual per saham sebelum entry.
PULLBACK_MIN_52W = 0.50
PULLBACK_MAX_52W = 0.95     # sudah pullback, bukan lagi nyaris di all-time-high
MA_PROXIMITY_PCT = 0.04     # toleransi 4% dari MA20/50/100 untuk dianggap "dekat support"
PULLBACK_TOP_N = 10         # batasi daftar pullback biar pesan tidak kepanjangan

TOP_N = 20
BATCH_SIZE = 50             # jumlah ticker per batch download
BATCH_DELAY = 2             # jeda antar batch (detik), hindari rate limit

NDX100_SOURCES = [
    "https://yfiua.github.io/index-constituents/constituents-nasdaq100.csv",
    # Catatan: source Gary-Strauss/nasdaq100-scraper dihapus per Agustus 2026 --
    # README repo tersebut menunjuk ke path yang sudah tidak berlaku (404), kemungkinan
    # repo di-rename tapi dokumentasinya belum di-update. yfiua terbukti stabil (101 ticker).
]

# Kelas saham ganda dari perusahaan yang sama -> canonical ticker yang dipertahankan
DUPLICATE_SHARE_CLASSES = {
    "GOOG": "GOOGL",   # Alphabet: pertahankan GOOGL (Class A)
    "FOX": "FOXA",     # Fox Corp: pertahankan FOXA (Class A), jaga-jaga kalau masuk index
    "NWS": "NWSA",     # News Corp: pertahankan NWSA (Class A), jaga-jaga kalau masuk index
}


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
# UNIVERSE (NASDAQ 100)
# =========================

def load_ndx100_tickers():
    for url in NDX100_SOURCES:
        try:
            df = pd.read_csv(url)
            col = "Symbol" if "Symbol" in df.columns else df.columns[0]

            tickers = (
                df[col]
                .astype(str)
                .str.strip()
                .str.replace(".", "-", regex=False)  # format yfinance
                .tolist()
            )
            tickers = [t for t in tickers if t and t.upper() == t and len(t) <= 6]

            if len(tickers) >= 90:  # sanity check, NDX100 harusnya ~100-102
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
                    sub = sub.dropna(subset=["Close"])  # buang baris hari ini yang belum ada datanya (pre-market)
                    if not sub.empty and len(sub) >= MIN_HISTORY_DAYS:
                        all_data[t] = sub
                except Exception:
                    continue

        except Exception as e:
            print(f"Batch {batch_num} error: {e}")

        time.sleep(delay)

    print(f"Total ticker dengan data valid: {len(all_data)}/{len(tickers)}")
    return all_data


def download_single(ticker, max_retries=3, retry_delay=20):
    """
    Dipakai khusus untuk benchmark -- kalau ini gagal, SELURUH run mati (fail-fast by design).
    Makanya dikasih retry+backoff, beda dengan ticker individual di batch yang cukup di-skip
    kalau gagal.
    """
    for attempt in range(1, max_retries + 1):
        try:
            df = yf.download(ticker, period="2y", interval="1d", auto_adjust=True, progress=False)
            if not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df = df.dropna(subset=["Close"])  # buang baris hari ini yang belum ada datanya (pre-market)
                if len(df) >= MIN_HISTORY_DAYS:
                    return df
                print(f"Data {ticker} kepanjangan kurang ({len(df)} baris)")
        except Exception as e:
            print(f"Percobaan {attempt}/{max_retries} gagal untuk {ticker}: {e}")

        if attempt < max_retries:
            print(f"Retry download {ticker} dalam {retry_delay} detik...")
            time.sleep(retry_delay)

    print(f"Gagal download {ticker} setelah {max_retries} percobaan.")
    return None


# =========================
# METRICS - MOMENTUM & MAIN FILTER
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
    """Berbasis closing price (bukan intraday High) -> lebih stabil untuk batch download."""
    high_52w = df["Close"].tail(252).max()
    close_now = df["Close"].iloc[-1]
    return close_now / high_52w, high_52w, close_now


def detect_breakout(df, lookback=BREAKOUT_LOOKBACK, volume_threshold=BREAKOUT_VOLUME_THRESHOLD):
    """
    Proxy sederhana untuk 'pivotal point breakout' ala Livermore:
    - Close hari ini adalah yang TERTINGGI dibanding N hari sebelumnya (bukan termasuk hari ini)
    - Dikonfirmasi oleh volume hari ini >= threshold x rata-rata volume 20 hari
    """
    if len(df) < lookback + 2:
        return False, 0.0

    close_now = df["Close"].iloc[-1]
    recent_high = df["Close"].iloc[-lookback - 1:-1].max()  # N hari SEBELUM hari ini
    is_new_high = close_now > recent_high

    volume_today = df["Volume"].iloc[-1]
    avg_volume_20d = df["Volume"].rolling(20).mean().iloc[-1]
    volume_ratio = volume_today / avg_volume_20d if avg_volume_20d > 0 else 0
    volume_confirmed = volume_ratio >= volume_threshold

    return (is_new_high and volume_confirmed), volume_ratio


# =========================
# METRICS - PULLBACK SETUP
# =========================

def calculate_moving_averages(df):
    ma20 = df["Close"].rolling(20).mean().iloc[-1]
    ma50 = df["Close"].rolling(50).mean().iloc[-1]
    ma100 = df["Close"].rolling(100).mean().iloc[-1]
    ma200 = df["Close"].rolling(200).mean().iloc[-1]
    return ma20, ma50, ma100, ma200


def detect_pullback_setup(df, benchmark_momentum):
    """
    Jalur screening TERPISAH dari filter momentum utama (yang mensyaratkan 52W>=80%).
    Saham yang sedang pullback wajar tidak akan lolos filter itu, makanya perlu logic sendiri.

    Kriteria:
    1. Tetap 'pemenang': momentum 12-1 > benchmark (sama seperti filter utama)
    2. Sudah pullback: 52W strength di rentang PULLBACK_MIN_52W - PULLBACK_MAX_52W
       (proxy close/52w_high, BUKAN Fibonacci retracement sesungguhnya -- lihat catatan di CONFIG)
    3. Tren jangka panjang masih utuh: harga masih di atas MA200
    4. Dekat support MA20/50/100 (radius MA_PROXIMITY_PCT)

    Tidak ada syarat higher-low/pivot -- daftar hasil dimaksudkan untuk dicek manual
    satu per satu (chart, konteks fundamental) sebelum dipertimbangkan entry.
    """
    momentum = calculate_momentum_12_1(df)
    if momentum is None or momentum <= benchmark_momentum:
        return None  # bukan 'pemenang' menurut definisi kita

    strength_52w, high_52w, close_now = calculate_52w_strength(df)
    if not (PULLBACK_MIN_52W <= strength_52w <= PULLBACK_MAX_52W):
        return None

    ma20, ma50, ma100, ma200 = calculate_moving_averages(df)
    if pd.isna(ma200) or close_now < ma200:
        return None  # tren jangka panjang sudah rusak, skip

    near_ma = None
    for label, ma_val in [("MA20", ma20), ("MA50", ma50), ("MA100", ma100)]:
        if pd.notna(ma_val) and ma_val > 0 and abs(close_now - ma_val) / ma_val <= MA_PROXIMITY_PCT:
            near_ma = label
            break
    if near_ma is None:
        return None

    volatility = calculate_daily_volatility(df)
    if not volatility:
        return None

    return {
        "close": close_now,
        "strength_52w": strength_52w,
        "momentum_12_1": momentum,
        "near_ma": near_ma,
        "score": momentum / volatility,
    }


# =========================
# DEDUP KELAS SAHAM GANDA
# =========================

def dedupe_share_classes(results):
    """
    Kalau beberapa kelas saham dari perusahaan yang sama lolos filter (mis. GOOGL & GOOG),
    simpan cuma satu -> canonical ticker. `results` harus sudah ter-sort dari skor tertinggi.
    """
    seen = set()
    deduped = []
    for r in results:
        canonical = DUPLICATE_SHARE_CLASSES.get(r["ticker"], r["ticker"])
        if canonical in seen:
            continue
        seen.add(canonical)
        deduped.append(r)
    return deduped


# =========================
# SCREENER LOGIC - MOMENTUM UTAMA
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
        is_breakout, breakout_volume_ratio = detect_breakout(df)

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
            "is_breakout": is_breakout,
        }

    except Exception as e:
        print(f"Error analyze {ticker}: {e}")
        return None


# =========================
# MAIN
# =========================

def main():
    print("=" * 50)
    print("Running Momentum Screener - Nasdaq 100")
    print("=" * 50)

    tickers = load_ndx100_tickers()
    if not tickers:
        send_telegram("⚠️ Screener gagal: tidak bisa load daftar Nasdaq 100.")
        return

    # Benchmark (QQQE - equal weight, hindari bias mega-cap dari cap-weighted index)
    benchmark_df = download_single(BENCHMARK_TICKER)
    if benchmark_df is None:
        send_telegram(f"⚠️ Screener gagal: tidak bisa ambil data {BENCHMARK_TICKER}.")
        return

    benchmark_momentum = calculate_momentum_12_1(benchmark_df)
    if benchmark_momentum is None:
        send_telegram(f"⚠️ Screener gagal: data {BENCHMARK_TICKER} tidak cukup panjang.")
        return

    print(f"{BENCHMARK_TICKER} momentum 12-1: {benchmark_momentum:.2%}")

    # Data saham (batched)
    stock_data = download_batch(tickers)

    results = []
    pullback_candidates = []

    for ticker, df in stock_data.items():
        result = analyze_stock(ticker, df, benchmark_momentum)
        if result:
            results.append(result)

        pullback = detect_pullback_setup(df, benchmark_momentum)
        if pullback:
            pullback["ticker"] = ticker
            pullback_candidates.append(pullback)

    print(f"Total lolos filter momentum: {len(results)}")
    print(f"Total kandidat pullback setup: {len(pullback_candidates)}")

    results = sorted(results, key=lambda x: x["score"], reverse=True)
    results = dedupe_share_classes(results)
    top_results = results[:TOP_N]

    pullback_candidates = sorted(pullback_candidates, key=lambda x: x["score"], reverse=True)
    pullback_candidates = dedupe_share_classes(pullback_candidates)
    top_pullback = pullback_candidates[:PULLBACK_TOP_N]

    breakout_stocks = [r for r in top_results if r["is_breakout"]]

    today = datetime.now().strftime("%d %b %Y")

    if not top_results:
        message = (
            f"📈 <b>MOMENTUM SCREENER - NASDAQ 100</b>\n"
            f"{today}\n\n"
            f"Tidak ada saham yang lolos filter momentum.\n\n"
            f"Filter: 52W≥{MIN_52W_STRENGTH:.0%} | Momentum 12-1 > {BENCHMARK_TICKER} ({benchmark_momentum:.1%})"
        )
        send_telegram(message)
        return

    message = (
        f"📈 <b>MOMENTUM SCREENER - NASDAQ 100</b>\n"
        f"{today}\n"
        f"{BENCHMARK_TICKER} Momentum 12-1: {benchmark_momentum:+.1%}\n"
        f"Total lolos filter: {len(results)}\n"
    )

    # Section: sinyal breakout hari ini (actionable signal)
    if breakout_stocks:
        message += f"\n🎯 <b>BREAKOUT HARI INI ({len(breakout_stocks)}):</b>\n"
        for r in breakout_stocks:
            message += f"  • <b>{r['ticker']}</b> (vol {r['volume_ratio']:.1f}x)\n"
    else:
        message += "\n🎯 <i>Tidak ada breakout terdeteksi hari ini.</i>\n"

    # Section: pullback setup (higher low, dekat MA) -> watchlist tambahan, bukan sinyal langsung
    if top_pullback:
        message += f"\n📉 <b>PULLBACK SETUP ({len(top_pullback)}):</b>\n"
        for r in top_pullback:
            message += (
                f"  • <b>{r['ticker']}</b> | dekat {r['near_ma']} | "
                f"52W:{r['strength_52w']:.0%} | Mom12-1:{r['momentum_12_1']:+.1%}\n"
            )
    else:
        message += "\n📉 <i>Tidak ada setup pullback terdeteksi hari ini.</i>\n"

    message += (
        f"\n<b>#  Ticker  Score  52W   Mom12-1  vs {BENCHMARK_TICKER}</b>\n"
    )

    for i, r in enumerate(top_results, start=1):
        if r["is_breakout"]:
            signal_icon = "🎯"
        elif r["volume_ratio"] > 1.5:
            signal_icon = "🔥"
        else:
            signal_icon = ""

        message += (
            f"{i}. <b>{r['ticker']}</b> | "
            f"{r['score']:.1f} | "
            f"{r['strength_52w']:.0%} | "
            f"{r['momentum_12_1']:+.1%} | "
            f"{r['rs_vs_benchmark']:+.1%} {signal_icon}\n"
        )

    message += (
        f"\nScore = Momentum(12-1) / Volatilitas harian\n"
        f"🎯 = breakout hari ini (high {BREAKOUT_LOOKBACK}d baru + volume ≥{BREAKOUT_VOLUME_THRESHOLD}x)\n"
        f"🔥 = volume tinggi tanpa breakout\n"
        f"📉 = pullback setup: cek chart manual sebelum entry\n"
        f"Filter: 52W≥{MIN_52W_STRENGTH:.0%}, Momentum 12-1 &gt; {BENCHMARK_TICKER}"
    )

    send_telegram(message)
    print("Selesai.")


if __name__ == "__main__":
    main()
