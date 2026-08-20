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

    check_network()

    tickers, ticker_source = load_nasdaq100_tickers()
    print(f"Sumber ticker yang dipakai run ini: {ticker_source}")

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

    source_warning = f"\n⚠️ Sumber ticker: {ticker_source}" if ticker_source != "Wikipedia" else ""

    if not results:
        message = (
            f"📈 <b>LIVERMORE SCREENER - NASDAQ100</b>\n"
            f"{today}\n\n"
            f"Tidak ada saham yang lolos.\n\n"
            f"Filter: 52W ≥{MIN_52W_STRENGTH:.0%} | EMA50>150>200 | RS>QQQ"
            f"{source_warning}"
        )
        send_telegram(message)
        return

    message = (
        f"📈 <b>LIVERMORE SCREENER - NASDAQ100</b>\n"
        f"{today}\n"
        f"QQQ 6M: {benchmark_return_6m:.1%}\n\n"
        f"<b>Ticker | Score | 52W | RS vs QQQ | Vol</b>\n"
    )

    for i, r in enumerate(results[:TOP_N], start=1):
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
        f"Filter: 52W≥{MIN_52W_STRENGTH:.0%}, EMA trend, RS>QQQ"
        f"{source_warning}"
    )

    send_telegram(message)


if __name__ == "__main__":
    main()
