"""
DESK Scanner — pump.fun / Solana memecoin tarama botu
=======================================================
NE YAPAR:
  - DexScreener public API üzerinden Solana / pump.fun tokenlerini periyodik tarar
  - Yaş, hacim, işlem sayısı, bonding-curve ilerlemesi gibi filtreler uygular
  - (Opsiyonel) RugCheck public API ile mint/freeze authority ve risk skorunu çeker
  - Sonuçları terminale ve/veya Telegram'a "İZLE / GEÇ" etiketiyle bildirir

NE YAPMAZ (KASITLI OLARAK):
  - Trade açmaz, satın alma/satma yapmaz
  - Cüzdan bağlamaz, private key/seed İSTEMEZ ve kullanmaz
  - "Kesin al" demez — sadece filtrelenmiş veri + öneri seviyesi (GEÇ/İZLE) üretir
  - Rug/scam garantisi vermez; her token sıfırlanabilir kabul edilir

KURULUM:
  pip install requests

  (Opsiyonel Telegram bildirimi için)
  export TELEGRAM_BOT_TOKEN="..."
  export TELEGRAM_CHAT_ID="..."

ÇALIŞTIRMA:
  python desk_scanner.py

SORUMLULUK REDDİ:
  Bu araç sadece bilgi/filtreleme amaçlıdır. Yatırım tavsiyesi değildir.
  Solana memecoinlerinin büyük çoğunluğu değersizleşir. Kendi araştırmanı yap (DYOR).
"""

import time
import os
import requests
from datetime import datetime, timezone

# ------------------- AYARLAR (kendine göre değiştir) -------------------
POLL_INTERVAL_SEC = 60          # kaç saniyede bir tarasın
MAX_AGE_MINUTES = 10            # "yeni" kabul edilecek maksimum yaş
MIN_VOLUME_USD = 5000           # minimum hacim filtresi
MIN_TXNS = 100                  # minimum işlem sayısı filtresi
MAX_TOP10_HOLDER_PCT = 40       # top10 holder bu yüzdenin üstündeyse VETO (rugcheck varsa)
SEEN_FILE = "desk_seen_tokens.txt"   # aynı token'ı tekrar tekrar bildirmemek için

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
# ------------------------------------------------------------------------


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())


def mark_seen(address):
    with open(SEEN_FILE, "a") as f:
        f.write(address + "\n")


def fetch_pumpfun_pairs():
    """DexScreener'da 'pump.fun' etiketli / solana zincirindeki taze pariteleri çeker."""
    try:
        resp = requests.get(DEXSCREENER_SEARCH_URL, params={"q": "pump.fun"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("pairs", []) or []
    except Exception as e:
        print(f"[HATA] DexScreener çekilemedi: {e}")
        return []


def get_rugcheck_summary(mint_address):
    """Mümkünse mint/freeze authority + risk skoru çeker. Başarısız olursa None döner."""
    try:
        resp = requests.get(RUGCHECK_URL.format(mint=mint_address), timeout=10)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def age_minutes(pair_created_at_ms):
    if not pair_created_at_ms:
        return None
    created = datetime.fromtimestamp(pair_created_at_ms / 1000, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - created).total_seconds() / 60


def evaluate_candidate(pair):
    """DESK mantığı: varsayılan GEÇ, veri eksikse İZLE'ye bile çıkaramaz."""
    base = pair.get("baseToken", {})
    mint = base.get("address")
    symbol = base.get("symbol", "?")
    vol24h = pair.get("volume", {}).get("h24", 0) or 0
    txns24h = (pair.get("txns", {}).get("h24", {}) or {}).get("buys", 0) + \
              (pair.get("txns", {}).get("h24", {}) or {}).get("sells", 0)
    mcap = pair.get("marketCap") or pair.get("fdv") or 0
    liquidity = (pair.get("liquidity") or {}).get("usd", 0)
    created_ms = pair.get("pairCreatedAt")
    age = age_minutes(created_ms)

    veto_reasons = []
    if age is None or age > MAX_AGE_MINUTES:
        return None  # istenen yaş aralığının dışında, aday bile değil
    if vol24h < MIN_VOLUME_USD:
        veto_reasons.append("hacim düşük")
    if txns24h < MIN_TXNS:
        veto_reasons.append("işlem sayısı düşük")
    if not liquidity or liquidity < 2000:
        veto_reasons.append("likidite verisi yok/çok düşük")

    rc = get_rugcheck_summary(mint) if mint else None
    mint_auth = "doğrulanamadı"
    freeze_auth = "doğrulanamadı"
    top10pct = "doğrulanamadı"
    if rc:
        mint_auth = rc.get("mintAuthority", "doğrulanamadı")
        freeze_auth = rc.get("freezeAuthority", "doğrulanamadı")
        top10pct = rc.get("topHoldersPercent", "doğrulanamadı")
        if isinstance(top10pct, (int, float)) and top10pct > MAX_TOP10_HOLDER_PCT:
            veto_reasons.append(f"top10 holder %{top10pct} > limit")
        if mint_auth not in ("doğrulanamadı", None, "null", "revoked", False):
            veto_reasons.append("mint authority hâlâ aktif olabilir")
    else:
        veto_reasons.append("rugcheck verisi alınamadı -> otomatik VETO")

    decision = "GEÇ" if veto_reasons else "İZLE (manuel doğrulama şart)"

    return {
        "symbol": symbol,
        "mint": mint,
        "age_min": round(age, 1) if age is not None else None,
        "mcap": mcap,
        "liquidity": liquidity,
        "volume24h": vol24h,
        "txns24h": txns24h,
        "mint_authority": mint_auth,
        "freeze_authority": freeze_auth,
        "top10_holder_pct": top10pct,
        "veto_reasons": veto_reasons,
        "decision": decision,
        "url": pair.get("url"),
    }


def format_message(c):
    lines = [
        f"### {c['symbol']}",
        f"- Kontrat: {c['mint']}",
        f"- Yaş: {c['age_min']} dk",
        f"- Market cap: ${c['mcap']}",
        f"- Likidite: ${c['liquidity']}",
        f"- Hacim (24h): ${c['volume24h']}",
        f"- İşlem (24h): {c['txns24h']}",
        f"- Mint authority: {c['mint_authority']}",
        f"- Freeze authority: {c['freeze_authority']}",
        f"- Top10 holder %: {c['top10_holder_pct']}",
        f"- Veto sebepleri: {', '.join(c['veto_reasons']) if c['veto_reasons'] else 'yok'}",
        f"- Karar: {c['decision']}",
        f"- Link: {c['url']}",
    ]
    return "\n".join(lines)


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        print(f"[HATA] Telegram gönderilemedi: {e}")


def run_once(seen):
    pairs = fetch_pumpfun_pairs()
    new_candidates = []
    for pair in pairs:
        base = pair.get("baseToken", {})
        mint = base.get("address")
        if not mint or mint in seen:
            continue
        result = evaluate_candidate(pair)
        if result is None:
            continue
        new_candidates.append(result)
        seen.add(mint)
        mark_seen(mint)

    for c in new_candidates:
        msg = format_message(c)
        print(msg)
        print("-" * 50)
        send_telegram(msg)

    if not new_candidates:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Yeni aday yok.")


def main():
    print("DESK Scanner başladı. Ctrl+C ile durdur.")
    seen = load_seen()
    while True:
        run_once(seen)
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
