"""
DESK Scanner — pump.fun / Solana memecoin tarama botu (v2)
=======================================================
NE YAPAR:
  - pump.fun'ın herkese açık "yeni coinler" API'sinden en taze tokenleri çeker
  - Yaş ve market cap gibi filtreler uygular
  - (Opsiyonel) RugCheck public API ile mint/freeze authority ve risk skorunu çeker
  - Sonuçları terminale ve/veya Telegram'a "İZLE / GEÇ" etiketiyle bildirir

NE YAPMAZ (KASITLI OLARAK):
  - Trade açmaz, satın alma/satma yapmaz
  - Cüzdan bağlamaz, private key/seed İSTEMEZ ve kullanmaz
  - "Kesin al" demez — sadece filtrelenmiş veri + öneri seviyesi (GEÇ/İZLE) üretir
  - Rug/scam garantisi vermez; her token sıfırlanabilir kabul edilir

NOT (v1 -> v2 DEĞİŞİKLİĞİ):
  v1'de DexScreener'ın "search" endpoint'i kullanılıyordu, bu endpoint sadece
  isminde/sembolünde literal "pump.fun" geçen tokenleri buluyordu, bu yüzden
  hiç yeni aday bulamıyordu. v2'de pump.fun'ın kendi coin listesi API'si
  kullanılıyor (created_timestamp'e göre sıralı), bu gerçekten en yeni
  tokenleri döndürüyor.

KURULUM:
  pip install requests

  (Opsiyonel Telegram bildirimi için)
  TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID ortam değişkenlerini ayarla.

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
POLL_INTERVAL_SEC = 45          # kaç saniyede bir tarasın
MAX_AGE_MINUTES = 10            # "yeni" kabul edilecek maksimum yaş
MIN_MARKET_CAP_USD = 25000       # minimum market cap filtresi (çok düşükse muhtemelen ölü)
MIN_REPLIES = 5                 # pump.fun yorum sayısı - kaba bir ilgi göstergesi, 0 = filtre yok
MAX_TOP10_HOLDER_PCT = 40       # top10 holder bu yüzdenin üstündeyse VETO (rugcheck varsa)
SEEN_FILE = "desk_seen_tokens.txt"   # aynı token'ı tekrar tekrar bildirmemek için

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# pump.fun'ın herkese açık (resmi olmayan ama public) coin listesi API'si
# NOT: frontend-api.pump.fun Haziran 2026'da kapatıldı (DNS hatası / Cloudflare 530).
# Güncel adres frontend-api-v3.pump.fun. pump.fun bunu yine değiştirebilir;
# ileride tekrar 5xx/530 alırsan önce bu domain'in hâlâ geçerli olup olmadığını kontrol et.
PUMPFUN_COINS_URL = "https://frontend-api-v3.pump.fun/coins"
RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}
# ------------------------------------------------------------------------


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())


def mark_seen(address):
    with open(SEEN_FILE, "a") as f:
        f.write(address + "\n")


def fetch_pumpfun_new_coins():
    """pump.fun'dan en yeni coinleri (created_timestamp'e göre) çeker."""
    params = {
        "offset": 0,
        "limit": 50,
        "sort": "created_timestamp",
        "order": "DESC",
        "includeNsfw": "false",
    }
    try:
        resp = requests.get(PUMPFUN_COINS_URL, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json() or []
    except Exception as e:
        print(f"[HATA] pump.fun API çekilemedi: {e}")
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


def age_minutes(created_timestamp_ms):
    if not created_timestamp_ms:
        return None
    created = datetime.fromtimestamp(created_timestamp_ms / 1000, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - created).total_seconds() / 60


def evaluate_candidate(coin):
    """DESK mantığı: varsayılan GEÇ, veri eksikse İZLE'ye bile çıkaramaz."""
    mint = coin.get("mint")
    symbol = coin.get("symbol", "?")
    name = coin.get("name", "?")
    created_ts = coin.get("created_timestamp")
    age = age_minutes(created_ts)
    mcap = coin.get("usd_market_cap") or coin.get("market_cap") or 0
    complete = coin.get("complete", False)  # True ise zaten Raydium'a graduate olmuş
    replies = coin.get("reply_count", 0) or 0
    king_of_hill = coin.get("king_of_the_hill_timestamp")

    if age is None or age > MAX_AGE_MINUTES:
        return None  # istenen yaş aralığının dışında, aday bile değil

    veto_reasons = []
    if mcap < MIN_MARKET_CAP_USD:
        veto_reasons.append(f"market cap çok düşük (${mcap:.0f})")
    if replies < MIN_REPLIES:
        veto_reasons.append("ilgi/yorum sayısı düşük")

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
        "name": name,
        "mint": mint,
        "age_min": round(age, 1) if age is not None else None,
        "mcap": mcap,
        "graduated": complete,
        "replies": replies,
        "king_of_hill": bool(king_of_hill),
        "mint_authority": mint_auth,
        "freeze_authority": freeze_auth,
        "top10_holder_pct": top10pct,
        "veto_reasons": veto_reasons,
        "decision": decision,
        "pumpfun_url": f"https://pump.fun/coin/{mint}" if mint else None,
        "dexscreener_url": f"https://dexscreener.com/solana/{mint}" if mint else None,
    }


def format_message(c):
    lines = [
        f"### {c['symbol']} ({c['name']})",
        f"- Kontrat: {c['mint']}",
        f"- Yaş: {c['age_min']} dk",
        f"- Market cap: ${c['mcap']:.0f}",
        f"- Graduate oldu mu (Raydium): {'Evet' if c['graduated'] else 'Hayır, hâlâ bonding curve'}",
        f"- Yorum sayısı: {c['replies']}",
        f"- King of the Hill: {'Evet' if c['king_of_hill'] else 'Hayır'}",
        f"- Mint authority: {c['mint_authority']}",
        f"- Freeze authority: {c['freeze_authority']}",
        f"- Top10 holder %: {c['top10_holder_pct']}",
        f"- Veto sebepleri: {', '.join(c['veto_reasons']) if c['veto_reasons'] else 'yok'}",
        f"- Karar: {c['decision']}",
        f"- pump.fun: {c['pumpfun_url']}",
        f"- DexScreener: {c['dexscreener_url']}",
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
    coins = fetch_pumpfun_new_coins()
    new_candidates = []
    for coin in coins:
        mint = coin.get("mint")
        if not mint or mint in seen:
            continue
        result = evaluate_candidate(coin)
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
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Yeni aday yok. (Toplam çekilen coin: {len(coins)})")


def main():
    print("DESK Scanner v2 başladı. Ctrl+C ile durdur.")
    seen = load_seen()
    while True:
        run_once(seen)
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
