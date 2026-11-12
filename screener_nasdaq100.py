import os
import io
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

MIN_52W_STRENGTH = 0.80
TOP_N = 20

# =========================
# BOBOT SCORING
# =========================
# Total selalu 100. Direvisi dari versi awal karena RS vs QQQ (sinyal inti
# strategi momentum/Livermore) terlalu diremehkan: bobotnya kecil (15) DAN
# cepat saturasi di cap 30pp, sehingga saham dgn RS +72% dan +119% dapat
# skor RS yang nyaris sama. EMA-passing bonus (35, fixed) tidak diubah jadi
# bergradasi berdasarkan lebar EMA, karena itu akan mengunggulkan saham yang
# sudah "extended" (naik lurus tanpa jeda) dan menghukum saham yang sedang
# konsolidasi sehat sebelum breakout -- bertentangan dengan konsep pivotal
# point ala Livermore. Jadi EMA tetap fixed, cuma bobotnya dikurangi supaya
# tidak mendominasi skor.
WEIGHT_52W = 40          # kedekatan ke 52-week high
WEIGHT_EMA_PASS = 20     # bonus fixed untuk lolos filter EMA50>150>200 (turun dari 35)
WEIGHT_RS = 30           # relative strength vs QQQ (naik dari 15)
RS_SATURATION_PP = 0.75  # excess return utk skor RS penuh: 75pp (naik dari 30pp)
WEIGHT_VOLUME = 10       # rasio volume vs rata-rata 20 hari

# Fallback ticker list dipakai HANYA jika semua sumber online gagal diakses
# (mis. tidak ada koneksi internet sama sekali). Boleh basi, karena ini
# jalur darurat terakhir, bukan sumber utama.
FALLBACK_TICKERS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "AVGO", "GOOGL", "GOOG", "TSLA", "COST",
    "NFLX", "PLTR", "AMD", "CSCO", "TMUS", "LIN", "PEP", "ISRG", "INTU", "QCOM",
    "BKNG", "TXN", "AMGN", "AMAT", "ADBE", "GILD", "HON", "VRTX", "PANW", "CMCSA",
    "ADP", "MELI", "SBUX", "MU", "LRCX", "KLAC", "ADI", "CRWD", "CDNS", "SNPS",
    "MAR", "CEG", "ORLY", "MDLZ", "REGN", "DASH", "CTAS", "PYPL", "ABNB", "FTNT",
    "MRVL", "CSX", "WDAY", "ADSK", "ROP", "NXPI", "PCAR", "MNST", "CPRT", "PAYX",
    "AEP", "ROST", "CHTR", "FAST", "KDP", "EXC", "AZN", "KHC", "MCHP", "EA",
    "ODFL", "IDXX", "DDOG", "TTWO", "FANG", "BKR", "GEHC", "TEAM", "XEL", "CCEP",
    "DXCM", "CTSH", "ZS", "ANSS", "ON", "BIIB", "GFS", "CDW", "MDB", "DLTR",
    "TTD", "WBD", "ILMN", "MRNA", "SIRI", "WBA"
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

    response = requests.post(url, data=payload, timeout=20)

    print(f"Telegram status: {response.status_code}")
    print(response.text)


# =========================
# NETWORK DIAGNOSTIC
# =========================

DIAGNOSTIC_TARGETS = [
    ("Wikipedia API", "https://en.wikipedia.org/w/api.php?action=parse&page=Nasdaq-100&format=json&prop=text"),
    ("GitHub Pages mirror", "https://yfiua.github.io/index-constituents/constituents-nasdaq100.csv"),
    ("Yahoo Finance", "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"),
    ("Telegram API", "https://api.telegram.org"),
]


def check_network():
    """Cek konektivitas ke semua domain yang dipakai script ini.
    Hasilnya di-print ke log (Railway Deploy Logs) supaya begitu ada
    ticker yang error/basi, kita langsung tahu domain mana yang bermasalah
    tanpa perlu deploy terpisah untuk diagnostik."""
    print("=" * 50)
    print("NETWORK CHECK")
    print("=" * 50)

    results = {}
    for name, url in DIAGNOSTIC_TARGETS:
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (livermore-screener/1.0)"},
                timeout=10,
            )
            ok = 200 <= resp.status_code < 300
            results[name] = ok
            icon = "✅" if ok else "⚠️"
            print(f"{icon} {name}: status {resp.status_code}")
        except Exception as e:
            results[name] = False
            print(f"❌ {name}: GAGAL - {type(e).__name__}: {e}")

    print("=" * 50)
    return results


# =========================
# DATA HELPER
# =========================

def _clean_tickers(raw_tickers):
    """Bersihkan dan normalisasi list ticker (uppercase, buang duplikat,
    ganti '.' jadi '-' seperti format yfinance, misal BRK.B -> BRK-B)."""
    cleaned = []
    seen = set()
    for t in raw_tickers:
        if not isinstance(t, str):
            continue
        t = t.strip().upper().replace(".", "-")
        if t and t not in seen:
            seen.add(t)
            cleaned.append(t)
    return cleaned


def _load_from_wikipedia():
    """Sumber utama: tabel komponen Nasdaq-100 di Wikipedia.

    Dipakai lewat MediaWiki API resmi (action=parse) alih-alih fetch
    langsung ke halaman biasa. Fetch langsung ke halaman kadang kena
    soft-block/anti-bot dari IP datacenter (mis. Railway) yang tetap
    balas status 200 tapi kontennya bukan artikel penuh, sehingga
    parsing gagal walau statusnya "sukses". Lewat API JSON lebih
    konsisten karena tidak melalui pipeline anti-bot yang sama."""
    api_url = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "parse",
        "page": "Nasdaq-100",
        "format": "json",
        "prop": "text",
        "redirects": 1,
    }
    headers = {"User-Agent": "Mozilla/5.0 (livermore-screener/1.0; contact: n/a)"}

    resp = requests.get(api_url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise ValueError(f"Wikipedia API error: {data['error']}")

    html = data["parse"]["text"]["*"]
    tables = pd.read_html(io.StringIO(html))

    # Cari tabel yang punya kolom "Ticker" DAN "Company" (bukan tabel
    # histori perubahan yang juga bisa mengandung kata "Ticker" di header
    # bertingkat).
    candidate_columns = []
    for df in tables:
        cols = [str(c).strip() for c in df.columns]
        candidate_columns.append(cols[:6])  # simpan buat log jika gagal
        if "Ticker" in cols and "Company" in cols:
            tickers = df["Ticker"].astype(str).tolist()
            tickers = _clean_tickers(tickers)
            if len(tickers) >= 90:  # sanity check, Nasdaq-100 ~ 100-102 anggota
                return tickers

    # Kalau sampai sini berarti gagal -> log kolom2 tabel yang ditemukan
    # supaya kelihatan di Railway logs kenapa gagal (struktur berubah? dsb.)
    print(f"[debug] Jumlah tabel ditemukan: {len(tables)}")
    for i, cols in enumerate(candidate_columns[:8]):
        print(f"[debug] Tabel #{i} kolom: {cols}")

    raise ValueError("Tabel komponen dengan kolom 'Ticker' + 'Company' tidak ditemukan.")


def _load_from_github_mirror():
    """Sumber cadangan: mirror JSON/CSV dari yfiua/index-constituents,
    di-update otomatis tiap bulan (biasanya tanggal 1). Kode index untuk
    Nasdaq-100 di repo ini adalah 'nasdaq100' (bukan 'ndx')."""
    url = "https://yfiua.github.io/index-constituents/constituents-nasdaq100.csv"
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()

    df = pd.read_csv(io.StringIO(resp.text))

    # Coba beberapa kemungkinan nama kolom
    for col in ["Symbol", "Ticker", "symbol", "ticker"]:
        if col in df.columns:
            tickers = _clean_tickers(df[col].astype(str).tolist())
            if len(tickers) >= 90:
                return tickers

    raise ValueError("Kolom ticker tidak ditemukan di mirror GitHub.")


def load_nasdaq100_tickers():
    """Ambil daftar konstituen Nasdaq-100 terkini secara otomatis.
    Urutan sumber: Wikipedia -> mirror GitHub -> fallback list statis (basi).

    Return: (tickers, source_label) supaya caller bisa log/tampilkan
    sumber mana yang benar-benar dipakai pada run ini."""

    try:
        tickers = _load_from_wikipedia()
        print(f"Total Nasdaq 100 tickers loaded dari Wikipedia: {len(tickers)}")
        return tickers, "Wikipedia"
    except Exception as e:
        print(f"Gagal ambil ticker dari Wikipedia: {e}")

    try:
        tickers = _load_from_github_mirror()
        print(f"Total Nasdaq 100 tickers loaded dari mirror GitHub: {len(tickers)}")
        return tickers, "GitHub mirror"
    except Exception as e:
        print(f"Gagal ambil ticker dari mirror GitHub: {e}")

    print(f"⚠️ Semua sumber online gagal. Pakai fallback list statis ({len(FALLBACK_TICKERS)} ticker, mungkin sudah basi).")
    return FALLBACK_TICKERS, "FALLBACK STATIS (basi)"


PRICE_COLUMNS = ["Open", "High", "Low", "Close"]


def get_data(ticker):
    """Download data harian 2 tahun dan bersihkan bar yang rusak.

    REVISI: dengan auto_adjust=True, yfinance menyesuaikan harga memakai
    Adj Close. Kalau Yahoo belum mengisi Adj Close untuk bar terbaru,
    seluruh OHLC di bar itu jadi NaN sementara Volume tetap terisi.
    Bar seperti itu dibuang supaya tidak meracuni perhitungan
    (52W strength, return 6M, EMA)."""
    df = yf.download(
        ticker,
        period="2y",
        interval="1d",
        auto_adjust=True,
        progress=False
    )

    if df is None or df.empty:
        return None

    # Ratakan kolom MultiIndex DULU sebelum akses kolom per nama
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    missing_cols = [c for c in PRICE_COLUMNS + ["Volume"] if c not in df.columns]
    if missing_cols:
        print(f"[warn] {ticker}: kolom hilang {missing_cols}")
        return None

    rows_before = len(df)
    df = df.dropna(subset=PRICE_COLUMNS)
    dropped = rows_before - len(df)
    if dropped > 0:
        print(f"[warn] {ticker}: {dropped} bar dengan harga NaN dibuang")

    if len(df) < 220:
        return None

    return df


def calculate_return_6m(df):
    if len(df) < 126:
        return float("nan")
    close_now = df["Close"].iloc[-1]
    close_6m = df["Close"].iloc[-126]
    return (close_now / close_6m) - 1


def _is_valid_number(x):
    return x is not None and not pd.isna(x)


# =========================
# SCREENER LOGIC
# =========================

def analyze_stock(ticker, benchmark_return_6m, benchmark_last_date):
    df = get_data(ticker)

    if df is None:
        print(f"{ticker} SKIP | data kosong/kurang dari 220 bar")
        return None, "no_data"

    last_date = df.index[-1].date()

    # Data saham tertinggal dari QQQ -> bandingkan apel dengan jeruk.
    # Tetap dianalisis, tapi ditandai supaya kelihatan di log & Telegram.
    is_stale = last_date < benchmark_last_date
    if is_stale:
        print(f"[warn] {ticker}: bar terakhir {last_date} tertinggal dari QQQ ({benchmark_last_date})")

    close = df["Close"].iloc[-1]
    volume_today = df["Volume"].iloc[-1]

    high_52w = df["High"].tail(252).max()
    strength_52w = close / high_52w if high_52w > 0 else float("nan")

    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()
    df["EMA150"] = df["Close"].ewm(span=150, adjust=False).mean()
    df["EMA200"] = df["Close"].ewm(span=200, adjust=False).mean()

    ema50 = df["EMA50"].iloc[-1]
    ema150 = df["EMA150"].iloc[-1]
    ema200 = df["EMA200"].iloc[-1]

    stock_return_6m = calculate_return_6m(df)

    avg_volume_20d = df["Volume"].rolling(20).mean().iloc[-1]
    if _is_valid_number(avg_volume_20d) and avg_volume_20d > 0 and _is_valid_number(volume_today):
        volume_ratio = volume_today / avg_volume_20d
    else:
        volume_ratio = 0
    volume_active = volume_ratio > 1

    # Guard: kalau masih ada NaN di angka kunci, jangan diam-diam dianggap
    # "gagal filter" -- tandai sebagai data invalid supaya kelihatan.
    key_values = {
        "close": close,
        "high_52w": high_52w,
        "ema50": ema50,
        "ema150": ema150,
        "ema200": ema200,
        "return_6m": stock_return_6m,
    }
    invalid = [k for k, v in key_values.items() if not _is_valid_number(v)]
    if invalid:
        print(f"{ticker} SKIP | data invalid (NaN): {invalid}")
        return None, "invalid"

    passed_52w = strength_52w >= MIN_52W_STRENGTH
    passed_ema = ema50 > ema150 > ema200
    passed_rs = stock_return_6m > benchmark_return_6m

    if passed_52w and passed_ema and passed_rs:
        score = (
            min(strength_52w / 1.0, 1) * WEIGHT_52W +
            WEIGHT_EMA_PASS +
            min((stock_return_6m - benchmark_return_6m) / RS_SATURATION_PP, 1) * WEIGHT_RS +
            min(volume_ratio / 2, 1) * WEIGHT_VOLUME
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
            "last_date": last_date,
            "is_stale": is_stale,
            "score": round(score, 1)
        }, "passed"

    print(
        f"{ticker} CHECK | "
        f"52W: {passed_52w} ({strength_52w:.1%}) | "
        f"EMA: {passed_ema} | "
        f"RS: {passed_rs} "
        f"({stock_return_6m:.1%} vs QQQ {benchmark_return_6m:.1%}) | "
        f"VOL: {volume_ratio:.2f}x"
    )

    return None, "failed"


# =========================
# MAIN
# =========================

def main():
    print("Running Livermore Screener - Nasdaq 100...")

    check_network()

    tickers, ticker_source = load_nasdaq100_tickers()
    print(f"Sumber ticker yang dipakai run ini: {ticker_source}")

    benchmark_df = get_data(BENCHMARK_TICKER)
    if benchmark_df is None:
        print("Gagal mengambil data QQQ.")
        send_telegram("⚠️ Gagal mengambil data QQQ dari yfinance. Screener dibatalkan.")
        return

    print("[debug] 3 bar terakhir QQQ:")
    print(benchmark_df.tail(3))

    benchmark_return_6m = calculate_return_6m(benchmark_df)
    benchmark_last_date = benchmark_df.index[-1].date()
    data_date_str = benchmark_df.index[-1].strftime("%d %b %Y")

    # REVISI: kalau return QQQ NaN, semua perbandingan RS otomatis False
    # dan hasilnya "tidak ada saham lolos" yang menyesatkan. Batalkan saja.
    if not _is_valid_number(benchmark_return_6m):
        print("Return 6M QQQ = NaN. Screener dibatalkan.")
        send_telegram(
            "⚠️ <b>LIVERMORE SCREENER</b>\n"
            "Return 6M QQQ = NaN (data yfinance rusak). "
            "Screener dibatalkan supaya tidak mengirim hasil palsu."
        )
        return

    print(f"QQQ return 6M: {benchmark_return_6m:.2%} (bar terakhir: {benchmark_last_date})")

    results = []
    status_count = {"passed": 0, "failed": 0, "invalid": 0, "no_data": 0, "error": 0}

    for ticker in tickers:
        print(f"Processing {ticker}...")
        try:
            result, status = analyze_stock(ticker, benchmark_return_6m, benchmark_last_date)
            status_count[status] += 1
            if result:
                print(f"PASSED: {ticker}")
                results.append(result)
            else:
                print(f"FAILED: {ticker}")
        except Exception as e:
            status_count["error"] += 1
            print(f"ERROR {ticker}: {e}")

    print(f"Total passed: {len(results)}")
    print(f"Ringkasan status: {status_count}")

    results = sorted(results, key=lambda x: x["score"], reverse=True)

    run_time = datetime.now().strftime("%d %b %Y %H:%M")

    source_warning = f"\n⚠️ Sumber ticker: {ticker_source}" if ticker_source != "Wikipedia" else ""

    bad_data = status_count["invalid"] + status_count["no_data"] + status_count["error"]
    data_warning = (
        f"\n⚠️ {bad_data} dari {len(tickers)} ticker datanya tidak valid/kosong/error"
        if bad_data > 0 else ""
    )

    header = (
        f"📈 <b>LIVERMORE SCREENER - NASDAQ100</b>\n"
        f"Data close: {data_date_str}\n"
        f"Run: {run_time}\n"
        f"QQQ 6M: {benchmark_return_6m:.1%}\n\n"
    )

    if not results:
        message = (
            header +
            f"Tidak ada saham yang lolos.\n\n"
            f"Filter: 52W ≥{MIN_52W_STRENGTH:.0%} | EMA50>150>200 | RS>QQQ"
            f"{data_warning}"
            f"{source_warning}"
        )
        send_telegram(message)
        return

    message = header + f"<b>Ticker | Score | 52W | RS vs QQQ | Vol</b>\n"

    for i, r in enumerate(results[:TOP_N], start=1):
        rs_vs_benchmark = r["return_6m"] - r["benchmark_return_6m"]
        volume_icon = "🔥" if r["volume_active"] else ""
        stale_icon = " ⏳" if r["is_stale"] else ""

        message += (
            f"{i}. <b>{r['ticker']}</b>{stale_icon} | "
            f"{r['score']:.0f} | "
            f"{r['strength_52w']:.0%} | "
            f"{rs_vs_benchmark:+.1%} | "
            f"{r['volume_ratio']:.1f}x {volume_icon}\n"
        )

    stale_note = "\n⏳ = data saham tertinggal dari QQQ" if any(r["is_stale"] for r in results[:TOP_N]) else ""

    message += (
        f"\nTotal: {len(results)} saham\n"
        f"Filter: 52W≥{MIN_52W_STRENGTH:.0%}, EMA trend, RS>QQQ"
        f"{stale_note}"
        f"{data_warning}"
        f"{source_warning}"
    )

    send_telegram(message)


if __name__ == "__main__":
    main()
