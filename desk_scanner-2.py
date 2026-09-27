"""
DESK Scanner v4 — pump.fun / Solana memecoin tarama botu
=========================================================
Bu sürüm, kullanıcının verdiği somut kriter setini birebir uygular:

GEÇ say (herhangi biri varsa -> otomatik GEÇ):
  - unique alıcı < 5
  - yaş < 2 dk (henüz hayatta kalıp kalmayacağı belli değil)
  - yaş > MAX_AGE_MINUTES (artık "yeni" sayılmıyor)
  - bonding curve ilerlemesi > %40 (mezuniyete çok yakın, "yeni fırsat" değil)
  - metadata yok (isim/görsel/açıklama eksik -> boş/otomatik lansman şüphesi)
  - mint authority açık
  - freeze authority açık
  - creator/dev cüzdanı >= %25 tutuyor
  - top5 holder (bonding curve escrow'u hariç) >= %80
  - top10 holder (escrow hariç) > %40 (sert veto) / %20-40 arası = kırmızı bayrak (skor cezası)
  - ilk trade'lerde aynı slot'ta yoğun "bundle" alım tespit edildi (>= %25)
  - RugCheck raporu hiç alınamadı (satış yolu / mint / freeze doğrulanamaz)
  - RugCheck 'danger' seviyeli bir risk buldu (bunun içinde honeypot/satış-engeli
    türü bulgular da olabilir — RugCheck bunları otomatik risks[] listesine yazar)
  - RugCheck bu tokeni 'rugged' olarak işaretlemiş

Hiçbiri yoksa 0-100 kompozit bir skor hesaplanır; sadece skor eşiği de
geçilirse "İZLE (manuel doğrulama şart)" denir. Asla "AL" denmez, trade
açılmaz, cüzdan bağlanmaz.

VERİ KAYNAKLARI: pump.fun (coin listesi + trade akışı), RugCheck (authority/
holder/risk raporu). İkisi de resmi dokümante API değil; pump.fun zaman
zaman domain/şema değiştirebiliyor (bkz. Haziran 2026 kapanması).

SORUMLULUK REDDİ: Bilgi/filtreleme amaçlıdır, yatırım tavsiyesi değildir.
"""

import time
import os
import difflib
import requests
from datetime import datetime, timezone
from collections import Counter

# ============================== AYARLAR ==============================
POLL_INTERVAL_SEC = 60

# --- Yaş penceresi ---
MIN_AGE_MINUTES = 2          # bundan gençse "henüz hayatta kalıp kalmayacağı belli değil"
MAX_AGE_MINUTES = 15         # bundan yaşlıysa artık "yeni" sayılmıyor

# --- Bonding curve ---
# pump.fun coin objesinde net bir "% ilerleme" alanı yok; market cap'i tipik
# graduation eşiğine (~$69K) oranlayarak YAKLAŞIK hesaplıyoruz. Kesin değil,
# ama yön olarak doğru (DexScreener'daki "Progress" göstergesiyle aynı mantık).
GRADUATION_MARKET_CAP_USD_APPROX = 69000
MAX_BONDING_CURVE_PROGRESS_PCT = 40

# --- Alıcı / ilgi ---
MIN_UNIQUE_BUYERS = 5
MIN_MARKET_CAP_USD = 8000     # aşırı boş lansmanları da elemek için düşük bir taban

# --- Metadata ---
REQUIRE_METADATA = True       # isim + görsel + açıklama üçü de dolu olmalı

# --- Authority / holder / creator ---
MAX_CREATOR_HOLD_PCT = 25     # creator bu yüzdeyi TUTUYORSA -> veto (>=25)
MAX_TOP5_HOLDER_PCT = 80      # top5 (escrow hariç) bu yüzdeyi TUTUYORSA -> veto (>=80)
TOP10_REDFLAG_PCT = 20        # bu aralık kırmızı bayrak (skor cezası)
TOP10_HARD_VETO_PCT = 40      # bunun ÜSTÜ sert veto

# --- Bundle / cluster alım tespiti ---
FIRST_N_TRADES_FOR_BUNDLE_CHECK = 20   # ilk N trade içinde bak
BUNDLE_SAME_SLOT_VETO_PCT = 25         # bu N trade'in %X'i aynı slotta ise -> veto

# --- RugCheck genel risk skoru ---
MAX_RUGCHECK_RISK_SCORE = 55  # 0-100, yüksek = riskli

# --- Kopya isim tespiti ---
COPYCAT_SIMILARITY_THRESHOLD = 0.80
KNOWN_NAMES = [
    "bonk", "wif", "dogwifhat", "pepe", "trump", "boden", "slerf", "popcat",
    "mew", "goat", "pnut", "chillguy", "fartcoin", "moodeng", "act", "neiro",
    "turbo", "wojak", "doge", "shib", "floki", "myro", "bome", "samo", "jup",
    "raydium", "solana", "official", "elonmusk", "biden",
]

# İZLE diyebilmek için gereken minimum kompozit skor (0-100)
WATCH_SCORE_THRESHOLD = 65

SEEN_FILE = "desk_seen_tokens.txt"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

PUMPFUN_COINS_URL = "https://frontend-api-v3.pump.fun/coins"
PUMPFUN_CREATOR_COINS_URL = "https://frontend-api-v3.pump.fun/coins/user-created-coins/{creator}"
PUMPFUN_TRADES_URL = "https://frontend-api-v3.pump.fun/trades/all/{mint}"
RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}
# ======================================================================


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())


def mark_seen(address):
    with open(SEEN_FILE, "a") as f:
        f.write(address + "\n")


def age_minutes(created_timestamp_ms):
    if not created_timestamp_ms:
        return None
    created = datetime.fromtimestamp(created_timestamp_ms / 1000, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - created).total_seconds() / 60


# --------------------------- VERİ ÇEKME ---------------------------

def fetch_pumpfun_new_coins():
    params = {
        "offset": 0, "limit": 50,
        "sort": "created_timestamp", "order": "DESC",
        "includeNsfw": "false",
    }
    try:
        resp = requests.get(PUMPFUN_COINS_URL, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json() or []
    except Exception as e:
        print(f"[HATA] pump.fun coin listesi çekilemedi: {e}")
        return []


def fetch_creator_prior_coin_count(creator):
    if not creator:
        return None
    try:
        url = PUMPFUN_CREATOR_COINS_URL.format(creator=creator)
        resp = requests.get(url, params={"offset": 0, "limit": 50}, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json() or []
        return len(data)
    except Exception:
        return None


def fetch_trades(mint, limit=100):
    """Erken trade akışını çeker. Alınamazsa None döner (veri yok anlamında)."""
    if not mint:
        return None
    try:
        resp = requests.get(
            PUMPFUN_TRADES_URL.format(mint=mint),
            params={"limit": limit, "offset": 0}, headers=HEADERS, timeout=12,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("trades") or data.get("data") or []
        return data if isinstance(data, list) else None
    except Exception:
        return None


def fetch_rugcheck_report(mint):
    if not mint:
        return None
    try:
        resp = requests.get(RUGCHECK_URL.format(mint=mint), headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


# --------------------------- ANALİZ YARDIMCILARI ---------------------------

def analyze_trades(trades):
    """
    trades: pump.fun trade listesi (en yeni -> en eski veya tersi olabilir,
    sırayı garanti etmiyoruz, sadece sayım/gruplama yapıyoruz).
    Döner: (unique_buyers:int|None, bundle_pct:float|None)
    """
    if trades is None:
        return None, None
    if len(trades) == 0:
        return 0, 0.0

    buy_trades = [t for t in trades if t.get("is_buy", t.get("isBuy", True))]
    buyers = set()
    for t in buy_trades:
        user = t.get("user") or t.get("trader") or t.get("owner")
        if user:
            buyers.add(user)
    unique_buyers = len(buyers) if buyers else len(buy_trades)  # user alanı yoksa en azından trade sayısı

    # Bundle tespiti: ilk N trade içinde en kalabalık slot'un oranı
    sample = buy_trades[:FIRST_N_TRADES_FOR_BUNDLE_CHECK]
    slot_key = "slot" if sample and "slot" in sample[0] else None
    bundle_pct = None
    if slot_key and sample:
        slots = [t.get(slot_key) for t in sample if t.get(slot_key) is not None]
        if slots:
            counts = Counter(slots)
            most_common_count = counts.most_common(1)[0][1]
            bundle_pct = (most_common_count / len(slots)) * 100
    return unique_buyers, bundle_pct


def has_metadata(coin):
    name = (coin.get("name") or "").strip()
    image = (coin.get("image_uri") or "").strip()
    desc = (coin.get("description") or "").strip()
    return bool(name) and bool(image) and bool(desc)


def estimate_bonding_curve_progress_pct(mcap):
    if not mcap:
        return 0.0
    return min(100.0, (mcap / GRADUATION_MARKET_CAP_USD_APPROX) * 100)


def extract_authorities(report):
    if not report:
        return None, None
    token_obj = report.get("token") or {}
    mint_auth = token_obj.get("mintAuthority", report.get("mintAuthority"))
    freeze_auth = token_obj.get("freezeAuthority", report.get("freezeAuthority"))
    return mint_auth, freeze_auth


def get_non_escrow_holders(report, coin):
    """topHolders listesinden bonding-curve escrow hesaplarını çıkarır,
    kalanları pct'ye göre büyükten küçüğe sıralı döner."""
    if not report:
        return None
    holders = report.get("topHolders")
    if not isinstance(holders, list):
        return None
    escrow_addrs = {coin.get("bonding_curve"), coin.get("associated_bonding_curve")}
    escrow_addrs.discard(None)
    filtered = [h for h in holders if h.get("address") not in escrow_addrs and h.get("owner") not in escrow_addrs]
    filtered.sort(key=lambda h: float(h.get("pct", 0)), reverse=True)
    return filtered


def sum_pct(holders, n=None):
    if holders is None:
        return None
    subset = holders[:n] if n else holders
    try:
        return sum(float(h.get("pct", 0)) for h in subset)
    except Exception:
        return None


def get_creator_holding_pct(holders, creator):
    if holders is None or not creator:
        return None
    total = 0.0
    found = False
    for h in holders:
        if h.get("address") == creator or h.get("owner") == creator:
            try:
                total += float(h.get("pct", 0))
                found = True
            except Exception:
                pass
    return total if found else 0.0  # rapor var ama creator listede yoksa -> ihmal edilebilir pay varsayılır


def extract_rugcheck_score(report):
    if not report:
        return None
    val = report.get("score_normalised", report.get("score"))
    try:
        val = float(val)
    except (TypeError, ValueError):
        return None
    if val > 100:
        val = min(100.0, val / 100.0)
    return val


def extract_danger_risks(report):
    if not report:
        return []
    risks = report.get("risks") or []
    return [r.get("name", "bilinmeyen risk") for r in risks if r.get("level") == "danger"]


def is_copycat_name(name, symbol):
    text = f"{name} {symbol}".lower().strip()
    for known in KNOWN_NAMES:
        ratio = difflib.SequenceMatcher(None, text, known).ratio()
        if ratio >= COPYCAT_SIMILARITY_THRESHOLD or known in text:
            return known
    return None


# --------------------------- ANA DEĞERLENDİRME ---------------------------

def evaluate_candidate(coin):
    mint = coin.get("mint")
    symbol = coin.get("symbol", "?")
    name = coin.get("name", "?")
    creator = coin.get("creator")
    age = age_minutes(coin.get("created_timestamp"))
    mcap = coin.get("usd_market_cap") or coin.get("market_cap") or 0
    complete = coin.get("complete", False)

    if age is None or age > MAX_AGE_MINUTES:
        return None  # aday penceresinin tamamen dışında

    hard_vetoes = []
    score = 0.0
    score_notes = []

    # --- Yaş: en az 2 dk hayatta kalmış olmalı ---
    if age < MIN_AGE_MINUTES:
        hard_vetoes.append(f"yaş {age:.1f} dk < {MIN_AGE_MINUTES} dk (henüz hayatta kalıp kalmadığı belli değil)")

    # --- Market cap tabanı (boş lansman eleme) ---
    if mcap < MIN_MARKET_CAP_USD:
        hard_vetoes.append(f"market cap çok düşük (${mcap:.0f} < ${MIN_MARKET_CAP_USD})")

    # --- Bonding curve ilerlemesi (yaklaşık) ---
    curve_pct = estimate_bonding_curve_progress_pct(mcap) if not complete else 100.0
    if curve_pct > MAX_BONDING_CURVE_PROGRESS_PCT:
        hard_vetoes.append(f"bonding curve ~%{curve_pct:.0f} > %{MAX_BONDING_CURVE_PROGRESS_PCT} (mezuniyete yakın)")
    else:
        score += 10
        score_notes.append(f"+10 curve henüz erken (~%{curve_pct:.0f})")

    # --- Metadata ---
    if REQUIRE_METADATA and not has_metadata(coin):
        hard_vetoes.append("metadata eksik (isim/görsel/açıklamadan biri boş)")
    else:
        score += 5
        score_notes.append("+5 metadata tam")

    # --- Kopya isim ---
    copycat_hit = is_copycat_name(name, symbol)
    if copycat_hit:
        hard_vetoes.append(f"'{copycat_hit}' ismine/temasına aşırı benziyor (olası kopya/tuzak isim)")

    # --- Trade akışı: unique alıcı + bundle tespiti ---
    trades = fetch_trades(mint)
    unique_buyers, bundle_pct = analyze_trades(trades)
    if unique_buyers is None:
        hard_vetoes.append("trade/alıcı verisi alınamadı -> unique alıcı sayısı doğrulanamadı, otomatik VETO")
    elif unique_buyers < MIN_UNIQUE_BUYERS:
        hard_vetoes.append(f"unique alıcı {unique_buyers} < {MIN_UNIQUE_BUYERS}")
    else:
        bonus = min(15, unique_buyers)
        score += bonus
        score_notes.append(f"+{bonus} unique alıcı ({unique_buyers})")

    if bundle_pct is not None and bundle_pct >= BUNDLE_SAME_SLOT_VETO_PCT:
        hard_vetoes.append(f"ilk trade'lerin %{bundle_pct:.0f}'i aynı slotta -> güçlü bundle/rug sinyali")
    elif bundle_pct is not None:
        score += 5
        score_notes.append(f"+5 bundle belirtisi yok (aynı-slot oranı %{bundle_pct:.0f})")

    # --- Creator geçmişi (kaç coin çıkarmış) ---
    prior_count = fetch_creator_prior_coin_count(creator)
    if prior_count is not None:
        if prior_count == 0:
            score += 5
            score_notes.append("+5 creator'ın ilk coini")
        elif prior_count <= 15:
            score -= min(8, prior_count)
            score_notes.append(f"-{min(8, prior_count)} creator daha önce {prior_count} coin çıkarmış")
        else:
            hard_vetoes.append(f"creator {prior_count} coin çıkarmış -> seri/spam launcher paterni")

    # --- RugCheck: authority, holder, risk skoru ---
    report = fetch_rugcheck_report(mint)
    if report is None:
        hard_vetoes.append("RugCheck verisi alınamadı (mint/freeze/satış yolu doğrulanamadı) -> otomatik VETO")
        mint_auth_display = freeze_auth_display = top5_display = top10_display = rc_score_display = creator_pct_display = "doğrulanamadı"
    else:
        mint_auth, freeze_auth = extract_authorities(report)
        holders = get_non_escrow_holders(report, coin)
        top5pct = sum_pct(holders, 5)
        top10pct = sum_pct(holders, 10)
        creator_pct = get_creator_holding_pct(holders, creator)
        rc_risk_score = extract_rugcheck_score(report)
        danger_risks = extract_danger_risks(report)
        rugged_flag = bool(report.get("rugged"))

        mint_auth_display = mint_auth if mint_auth is not None else "revoked/null (iyi)"
        freeze_auth_display = freeze_auth if freeze_auth is not None else "revoked/null (iyi)"
        top5_display = f"%{top5pct:.1f}" if top5pct is not None else "doğrulanamadı"
        top10_display = f"%{top10pct:.1f}" if top10pct is not None else "doğrulanamadı"
        rc_score_display = f"{rc_risk_score:.0f}/100" if rc_risk_score is not None else "doğrulanamadı"
        creator_pct_display = f"%{creator_pct:.1f}" if creator_pct is not None else "doğrulanamadı"

        if rugged_flag:
            hard_vetoes.append("RugCheck bu tokeni 'rugged' olarak işaretlemiş")

        if mint_auth not in (None, "null", False):
            hard_vetoes.append("mint authority hâlâ aktif")
        else:
            score += 15
            score_notes.append("+15 mint authority revoked")

        if freeze_auth not in (None, "null", False):
            hard_vetoes.append("freeze authority hâlâ aktif")
        else:
            score += 15
            score_notes.append("+15 freeze authority revoked")

        if creator_pct is None:
            hard_vetoes.append("creator holding % doğrulanamadı -> otomatik VETO")
        elif creator_pct >= MAX_CREATOR_HOLD_PCT:
            hard_vetoes.append(f"creator %{creator_pct:.1f} tutuyor (>= %{MAX_CREATOR_HOLD_PCT})")
        else:
            score += 10
            score_notes.append(f"+10 creator payı makul (%{creator_pct:.1f})")

        if top5pct is None:
            hard_vetoes.append("top5 holder verisi doğrulanamadı -> otomatik VETO")
        elif top5pct >= MAX_TOP5_HOLDER_PCT:
            hard_vetoes.append(f"top5 holder %{top5pct:.1f} >= %{MAX_TOP5_HOLDER_PCT}")

        if top10pct is None:
            hard_vetoes.append("top10 holder verisi doğrulanamadı -> otomatik VETO")
        elif top10pct > TOP10_HARD_VETO_PCT:
            hard_vetoes.append(f"top10 holder %{top10pct:.1f} > %{TOP10_HARD_VETO_PCT} (sert limit)")
        elif top10pct > TOP10_REDFLAG_PCT:
            score -= 5
            score_notes.append(f"-5 top10 holder kırmızı bayrak bölgesinde (%{top10pct:.1f})")
        else:
            score += 10
            score_notes.append(f"+10 holder dağılımı sağlıklı (top10 %{top10pct:.1f})")

        if rc_risk_score is None:
            hard_vetoes.append("RugCheck risk skoru doğrulanamadı -> otomatik VETO")
        elif rc_risk_score > MAX_RUGCHECK_RISK_SCORE:
            hard_vetoes.append(f"RugCheck risk skoru çok yüksek ({rc_risk_score:.0f}/100)")
        else:
            bonus = max(0, 10 - (rc_risk_score / MAX_RUGCHECK_RISK_SCORE) * 10)
            score += bonus
            score_notes.append(f"+{bonus:.0f} RugCheck risk skoru kabul edilebilir ({rc_risk_score:.0f}/100)")

        if danger_risks:
            hard_vetoes.append(f"RugCheck 'danger' riskleri (honeypot/satış-engeli dahil olabilir): {', '.join(danger_risks)}")

    score = max(0.0, min(100.0, score))

    if hard_vetoes:
        decision = "GEÇ"
    elif score >= WATCH_SCORE_THRESHOLD:
        decision = "İZLE (manuel doğrulama şart)"
    else:
        decision = "GEÇ"
        hard_vetoes.append(f"kompozit skor eşiğin altında ({score:.0f}/100, gereken: {WATCH_SCORE_THRESHOLD})")

    return {
        "symbol": symbol, "name": name, "mint": mint,
        "age_min": round(age, 1), "mcap": mcap,
        "curve_pct": round(curve_pct, 1),
        "unique_buyers": unique_buyers, "bundle_pct": bundle_pct,
        "creator_prior_coins": prior_count,
        "mint_authority": mint_auth_display, "freeze_authority": freeze_auth_display,
        "creator_holding_pct": creator_pct_display,
        "top5_holder_pct": top5_display, "top10_holder_pct": top10_display,
        "rugcheck_risk_score": rc_score_display,
        "score": round(score, 1), "score_notes": score_notes,
        "hard_vetoes": hard_vetoes, "decision": decision,
        "pumpfun_url": f"https://pump.fun/coin/{mint}" if mint else None,
        "dexscreener_url": f"https://dexscreener.com/solana/{mint}" if mint else None,
    }


# --------------------------- BİLDİRİM ---------------------------

def format_message(c):
    lines = [
        f"### {c['symbol']} ({c['name']})  |  Skor: {c['score']}/100  |  Karar: {c['decision']}",
        f"- Kontrat: {c['mint']}",
        f"- Yaş: {c['age_min']} dk | Market cap: ${c['mcap']:.0f} | Curve ~%{c['curve_pct']}",
        f"- Unique alıcı: {c['unique_buyers']} | Bundle (aynı-slot) oranı: "
        f"{'%.0f%%' % c['bundle_pct'] if c['bundle_pct'] is not None else 'doğrulanamadı'}",
        f"- Creator önceki coin sayısı: {c['creator_prior_coins']}",
        f"- Mint authority: {c['mint_authority']} | Freeze authority: {c['freeze_authority']}",
        f"- Creator holding: {c['creator_holding_pct']} | Top5: {c['top5_holder_pct']} | Top10: {c['top10_holder_pct']}",
        f"- RugCheck risk skoru: {c['rugcheck_risk_score']}",
        f"- Skor detayı: {' | '.join(c['score_notes']) if c['score_notes'] else '-'}",
        f"- Sert veto sebepleri: {', '.join(c['hard_vetoes']) if c['hard_vetoes'] else 'yok'}",
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
        if c["decision"] != "GEÇ":
            send_telegram(msg)

    if not new_candidates:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Yeni aday yok. (Toplam çekilen coin: {len(coins)})")


def main():
    print("DESK Scanner v4 (detaylı kriter seti) başladı. Ctrl+C ile durdur.")
    seen = load_seen()
    consecutive_errors = 0
    while True:
        try:
            run_once(seen)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            print(f"[KRİTİK HATA] run_once patladı: {e}")
            backoff = min(POLL_INTERVAL_SEC * consecutive_errors, 300)
            print(f"[BİLGİ] {backoff} saniye bekleyip tekrar denenecek.")
            time.sleep(backoff)
            continue
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    import traceback
    try:
        main()
    except KeyboardInterrupt:
        print("Durduruldu.")
    except Exception:
        print("[KRİTİK HATA] Script beklenmedik şekilde durdu:")
        traceback.print_exc()
        print("[BİLGİ] 30 saniye bekleniyor, sonra container yeniden başlayacak.")
        time.sleep(30)
