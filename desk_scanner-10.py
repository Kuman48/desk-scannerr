"""
DESK Scanner v6 — pump.fun / Solana memecoin tarama botu (üç aşamalı: genç
takip -> tam değerlendirme -> momentum onayı)
=============================================================================
ÖNEMLİ DEĞİŞİKLİK (v5 -> v6): pump.fun'da saniyede birden fazla yeni coin
çıkıyor. "En yeni 50 coin" listesini her POLL_INTERVAL_SEC'te bir çekmek,
2 dakikayı yeni doldurmuş bir coin'in o listeden ÇOKTAN DÜŞMÜŞ olması
anlamına gelebiliyordu — yani coin gerçek yaşına (2 dk) ulaştığında bir daha
hiç görünmüyor, tam değerlendirme şansı hiç olmuyordu. v5'teki "tek veto
sebebi yaşsa kalıcı işaretleme" düzeltmesi bu yüzden yetersizdi.

v6'da üç aşama var:
  A) 'young' aşaması: coin ilk görüldüğünde MIN_AGE_MINUTES'i doldurmadıysa,
     RugCheck/creator-history gibi PAHALI sorgular yapılmadan sadece ucuza
     not ediliyor (mint + oluşturulma zamanı). Listeden düşse bile önemli
     değil çünkü artık listeye bağımlı değiliz.
  B) Coin gerçekten MIN_AGE_MINUTES'i doldurduğunda, mint adresinden TEKİL
     olarak (liste değil) tekrar çekilip TAM değerlendiriliyor (RugCheck
     dahil). Sert veto yoksa ve skor yeterliyse 'clean_watch' aşamasına
     geçiyor; değilse GEÇ (kalıcı).
  C) 'clean_watch' aşaması: RECHECK_DELAY_MINUTES sonra tekrar bakılıp mcap
     ve holder sayısı gerçekten büyümüş mü diye momentum kontrolü yapılıyor.
     Büyümüşse "İZLE (momentum onaylı)" denip Telegram'a gidiyor, değilse GEÇ
     (kağıt üstünde temiz ama sessizce ölmüş demektir).

Bu sürüm, kullanıcının verdiği somut kriter setini birebir uygular:

GEÇ say (herhangi biri varsa -> otomatik GEÇ):
  - farklı holder sayısı < 5 (bkz. NOT: gerçek "unique alıcı" değil, en yakın
    doğrulanabilir proxy)
  - yaş < 2 dk (henüz hayatta kalıp kalmayacağı belli değil)
  - yaş > MAX_AGE_MINUTES (artık "yeni" sayılmıyor)
  - bonding curve ilerlemesi > %40 (mezuniyete çok yakın, "yeni fırsat" değil)
  - metadata yok (isim/görsel/açıklama eksik -> boş/otomatik lansman şüphesi)
  - mint authority açık
  - freeze authority açık
  - creator/dev cüzdanı >= %25 tutuyor
  - top5 holder (bonding curve escrow'u hariç) >= %80
  - top10 holder (escrow hariç) > %40 (sert veto) / %20-40 arası = kırmızı bayrak (skor cezası)
  - RugCheck raporu hiç alınamadı (satış yolu / mint / freeze doğrulanamaz)
  - RugCheck 'danger' seviyeli bir risk buldu (bunun içinde honeypot/satış-engeli
    türü bulgular da olabilir — RugCheck bunları otomatik risks[] listesine yazar)
  - RugCheck bu tokeni 'rugged' olarak işaretlemiş

Hiçbiri yoksa 0-100 kompozit bir skor hesaplanır; sadece skor eşiği de
geçilirse "İZLE (manuel doğrulama şart)" denir. Asla "AL" denmez, trade
açılmaz, cüzdan bağlanmaz.

NOT (bundle/cluster tespiti kaldırıldı): pump.fun'ın gerçek trade akışı
endpoint'i JWT (oturum açmış hesap) authentication istiyor. Kimlik bilgisi
saklamadığımız/istemediğimiz için "ilk alıcılar aynı cluster mı" veya "aynı
slotta bundle alım var mı" sorusunu güvenilir şekilde cevaplayamıyoruz. Sahte
"bundle yok" demek yerine bu kontrolü dürüstçe kaldırdık; "farklı holder
sayısı" RugCheck'in holder listesinden geliyor ve gerçek trade/alıcı
sayısının yaklaşık bir proxy'sidir, birebir aynı şey değildir.

VERİ KAYNAKLARI: pump.fun (coin listesi), RugCheck (authority/holder/risk
raporu). İkisi de resmi dokümante API değil; pump.fun zaman zaman
domain/şema değiştirebiliyor (bkz. Haziran 2026 kapanması).

SORUMLULUK REDDİ: Bilgi/filtreleme amaçlıdır, yatırım tavsiyesi değildir.
"""

import time
import os
import json
import html
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
# NOT: pump.fun'ın gerçek "trade akışı" endpoint'i (frontend-api-v3.pump.fun/trades/...)
# JWT (oturum açmış hesap) authentication istiyor. Biz kimlik bilgisi saklamıyoruz/
# istemiyoruz, o yüzden gerçek "unique alıcı" sayısını çekemiyoruz. Bunun yerine
# RugCheck'in holder listesindeki FARKLI CÜZDAN SAYISINI kullanıyoruz — bu "kaç kişi
# aldı" ile birebir aynı şey değil ("kaç kişi şu an tutuyor" demek), ama halka açık
# ve gerçekten doğrulanabilir tek veri. Bu yaklaşıklığı mesajlarda açıkça belirtiyoruz.
MIN_DISTINCT_HOLDERS = 5
# NOT: pump.fun'da bir coin doğduğu an mcap'i sabit bonding-curve matematiği
# gereği ~28 SOL (bugünkü SOL fiyatıyla kabaca $3.000-4.000) oluyor. "Diğer
# botların kullandığı" diye kamuya açık/doğrulanabilir standart bir eşik
# YOK (her bot kendi eşiğini gizli tutuyor) — bu yüzden uydurmuyoruz. Bunun
# yerine doğal başlangıç değerinin HEMEN ÜSTÜNE bir taban koyuyoruz: amaç
# "hiç kimse almadı, tamamen ölü doğdu" launch'ları elemek, "henüz normal
# seyrediyor ama iki katına katlanmadı" coinleri değil. Eski değer ($8000)
# doğal başlangıcın 2 katından fazlasını şart koşuyordu ve neredeyse tüm
# 2 dakikalık adayları eliyordu.
MIN_MARKET_CAP_USD = 5000

# --- Metadata ---
REQUIRE_METADATA = True       # isim + görsel + açıklama üçü de dolu olmalı

# --- Authority / holder / creator ---
MAX_CREATOR_HOLD_PCT = 25     # creator bu yüzdeyi TUTUYORSA -> veto (>=25)
MAX_TOP5_HOLDER_PCT = 80      # top5 (escrow hariç) bu yüzdeyi TUTUYORSA -> veto (>=80)
TOP10_REDFLAG_PCT = 20        # bu aralık kırmızı bayrak (skor cezası)
TOP10_HARD_VETO_PCT = 40      # bunun ÜSTÜ sert veto

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

# --- İKİNCİ BAKIŞ (momentum onayı) ---
# Tek bir anlık fotoğraf (mint/freeze/holder temiz görünüyor) bir tokenin
# gerçekten hayatta kalacağını GARANTİ ETMEZ — sadece o an rug/honeypot
# olmadığını gösterir. İlgi çekmeden sessizce ölen "temiz görünümlü" tokenler
# de vardır. Bu yüzden ilk bakışta temiz + skor yeterli çıkan adaylar HEMEN
# İZLE ilan edilmiyor; RECHECK_DELAY_MINUTES kadar beklenip mcap/holder
# sayısı gerçekten BÜYÜMÜŞ mü diye ikinci kez kontrol ediliyor. Büyümemişse
# (yani ilgi sönmüşse) sonunda yine GEÇ deniyor.
RECHECK_DELAY_MINUTES = 5
MIN_MCAP_GROWTH_PCT = 15      # ikinci bakışta mcap en az bu kadar büyümüş olmalı
MIN_HOLDER_GROWTH = 3         # ikinci bakışta en az bu kadar yeni holder eklenmiş olmalı

SEEN_FILE = "desk_seen_tokens.txt"
PENDING_FILE = "desk_pending_recheck.json"   # ilk bakışta temiz çıkıp 2. bakış bekleyen adaylar

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

PUMPFUN_COINS_URL = "https://frontend-api-v3.pump.fun/coins"
PUMPFUN_COIN_BY_MINT_URL = "https://frontend-api-v3.pump.fun/coins/{mint}"  # ARTIK KULLANILMIYOR (bkz. not aşağıda)
PUMPFUN_CREATOR_COINS_URL = "https://frontend-api-v3.pump.fun/coins/user-created-coins/{creator}"
RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
# DexScreener herkese açık, kimlik/JWT gerektirmeyen public API — tekil coin'in
# TAZE market cap'ini almak için pump.fun'ın tekil endpoint'i yerine bunu
# kullanıyoruz (bkz. fetch_pumpfun_coin_by_mint docstring'i).
DEXSCREENER_TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/solana/{mint}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
    # pump.fun'ın bot/WAF koruması, Referer/Origin'i olmayan istekleri
    # (özellikle tekil /coins/{mint} rotasında) reddedebiliyor. Gerçek
    # bir tarayıcıdan pump.fun sitesi üzerinden gelmiş gibi görünmesi için.
    "Referer": "https://pump.fun/",
    "Origin": "https://pump.fun",
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


def load_pending():
    """İlk bakışta temiz çıkıp ikinci (momentum) bakışı bekleyen adaylar."""
    if not os.path.exists(PENDING_FILE):
        return {}
    try:
        with open(PENDING_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_pending(pending):
    try:
        with open(PENDING_FILE, "w") as f:
            json.dump(pending, f)
    except Exception as e:
        print(f"[HATA] Bekleme listesi kaydedilemedi: {e}")


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


def fetch_pumpfun_coin_by_mint(mint):
    """ARTIK KULLANILMIYOR — burada sadece belgesel amaçlı duruyor.

    Gerçek nedeni doğruladık: pump.fun'ın tekil /coins/{mint} rotası artık
    JWT (oturum açmış hesap) authentication istiyor (Authorization: Bearer
    <token> + sync=true parametresi) — WAF/bot koruması değil, doğrudan
    kimlik doğrulama gereksinimi. Bu yüzden HER istek (header'lar ne olursa
    olsun) 404/401 dönüyordu. DESK kimlik bilgisi saklamadığı/istemediği
    için bu rotayı KULLANAMAYIZ. Bunun yerine tekil coin'i güncellemek için
    fetch_dexscreener_market_data() (kimlik gerektirmeyen public API)
    kullanılıyor; coin'in isim/görsel/creator gibi değişmeyen alanları ise
    ilk görüldüğü andaki coin objesinden (pending içinde saklanan) geliyor."""
    if not mint:
        return None
    try:
        resp = requests.get(PUMPFUN_COIN_BY_MINT_URL.format(mint=mint), headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            print(f"[HATA] tekil coin çekilemedi (mint={mint}): HTTP {resp.status_code} "
                  f"- {resp.text[:200]}")
            return None
        return resp.json()
    except Exception as e:
        print(f"[HATA] tekil coin çekilemedi (mint={mint}): {type(e).__name__}: {e}")
        return None


def fetch_dexscreener_market_data(mint):
    """DexScreener'ın herkese açık, kimlik/JWT gerektirmeyen token-pairs
    API'sinden coin'in TAZE market cap'ini çeker. pump.fun'ın kendi tekil
    coin endpoint'i artık login gerektirdiği için (bkz. yukarıdaki not) bu
    tek güvenilir 'ikinci bakış' veri kaynağımız. Coin çok yeniyse (henüz
    hiç DEX/pump.fun pair'i indekslenmediyse) None dönebilir — bu durumda
    çağıran kod, ilk görüldüğü andaki mcap ile devam eder (tamamen veri
    kaybetmektense biraz bayat veri daha iyidir, ve bu açıkça belirtilir)."""
    if not mint:
        return None
    try:
        resp = requests.get(DEXSCREENER_TOKEN_PAIRS_URL.format(mint=mint), timeout=10)
        if resp.status_code != 200:
            return None
        pairs = resp.json()
        if not isinstance(pairs, list) or not pairs:
            return None
        # Birden fazla pair olabilir (ör. pump.fun bonding curve + graduation
        # sonrası Raydium); en yüksek likiditeli olanı esas alıyoruz.
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        mcap = best.get("marketCap") or best.get("fdv")
        return float(mcap) if mcap is not None else None
    except Exception as e:
        print(f"[HATA] DexScreener market verisi çekilemedi (mint={mint}): {type(e).__name__}: {e}")
        return None


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

    # --- Farklı holder sayısı: RugCheck raporu henüz gelmedi burada, o yüzden bu
    # kontrol RugCheck sonucunu aldıktan sonra, aşağıdaki blokta yapılıyor.

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
        distinct_holders_display = "doğrulanamadı"
        distinct_holder_count = None
    else:
        mint_auth, freeze_auth = extract_authorities(report)
        holders = get_non_escrow_holders(report, coin)
        top5pct = sum_pct(holders, 5)
        top10pct = sum_pct(holders, 10)
        creator_pct = get_creator_holding_pct(holders, creator)
        rc_risk_score = extract_rugcheck_score(report)
        danger_risks = extract_danger_risks(report)
        rugged_flag = bool(report.get("rugged"))
        distinct_holder_count = len(holders) if holders is not None else None

        mint_auth_display = mint_auth if mint_auth is not None else "revoked/null (iyi)"
        freeze_auth_display = freeze_auth if freeze_auth is not None else "revoked/null (iyi)"
        top5_display = f"%{top5pct:.1f}" if top5pct is not None else "doğrulanamadı"
        top10_display = f"%{top10pct:.1f}" if top10pct is not None else "doğrulanamadı"
        rc_score_display = f"{rc_risk_score:.0f}/100" if rc_risk_score is not None else "doğrulanamadı"
        creator_pct_display = f"%{creator_pct:.1f}" if creator_pct is not None else "doğrulanamadı"
        distinct_holders_display = str(distinct_holder_count) if distinct_holder_count is not None else "doğrulanamadı"

        if distinct_holder_count is None:
            hard_vetoes.append("farklı holder sayısı doğrulanamadı -> otomatik VETO")
        elif distinct_holder_count < MIN_DISTINCT_HOLDERS:
            hard_vetoes.append(f"farklı holder sayısı {distinct_holder_count} < {MIN_DISTINCT_HOLDERS}")
        else:
            bonus = min(15, distinct_holder_count)
            score += bonus
            score_notes.append(f"+{bonus} farklı holder sayısı yeterli ({distinct_holder_count})")

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
        "distinct_holders": distinct_holders_display,
        "distinct_holders_count": distinct_holder_count,
        "creator_prior_coins": prior_count,
        "mint_authority": mint_auth_display, "freeze_authority": freeze_auth_display,
        "creator_holding_pct": creator_pct_display,
        "top5_holder_pct": top5_display, "top10_holder_pct": top10_display,
        "rugcheck_risk_score": rc_score_display,
        "score": round(score, 1), "score_notes": score_notes,
        "hard_vetoes": hard_vetoes, "decision": decision,
        "pumpfun_url": f"https://pump.fun/coin/{mint}" if mint else None,
        "dexscreener_url": f"https://dexscreener.com/solana/{mint}" if mint else None,
        "image_uri": coin.get("image_uri"),
    }


# --------------------------- BİLDİRİM ---------------------------

def format_message(c):
    lines = [
        f"### {c['symbol']} ({c['name']})  |  Skor: {c['score']}/100  |  Karar: {c['decision']}",
        f"- Kontrat: {c['mint']}",
        f"- Yaş: {c['age_min']} dk | Market cap: ${c['mcap']:.0f} | Curve ~%{c['curve_pct']}",
    ]
    if c.get("momentum_note"):
        lines.append(f"- Momentum (2. bakış): {c['momentum_note']}")
    lines += [
        f"- Farklı holder sayısı (proxy, gerçek unique-alıcı değil): {c['distinct_holders']}",
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


def esc(value):
    """Telegram HTML parse_mode için güvenli kaçış (&, <, > karakterleri)."""
    return html.escape(str(value), quote=False)


def format_telegram_caption(c):
    """Kısa başlık: fotoğrafın altına (varsa) veya mesajın en üstüne gider."""
    is_watch = "İZLE" in c["decision"]
    emoji = "🟢" if is_watch else "🔴"
    return (
        f"{emoji} <b>{esc(c['symbol'])}</b>  (<i>{esc(c['name'])}</i>)\n"
        f"💯 <b>{c['score']}/100</b>  —  {esc(c['decision'])}\n"
        f"⏱ {c['age_min']} dk   💰 ${c['mcap']:.0f}   📈 curve ~%{c['curve_pct']}"
    )


def format_telegram_detail(c):
    """Uzun detay bloğu: emoji başlıklı bölümler + tıklanabilir linkler."""
    veto_text = esc(", ".join(c["hard_vetoes"])) if c["hard_vetoes"] else "yok"
    score_text = esc(" | ".join(c["score_notes"])) if c["score_notes"] else "-"
    lines = []
    if c.get("momentum_note"):
        lines += [f"📈 <b>Momentum (2. bakış):</b> {esc(c['momentum_note'])}", ""]
    lines += [
        "🔐 <b>Güvenlik</b>",
        f"• Mint authority: {esc(c['mint_authority'])}",
        f"• Freeze authority: {esc(c['freeze_authority'])}",
        f"• RugCheck riski: {esc(c['rugcheck_risk_score'])}",
        "",
        "👥 <b>Holder Dağılımı</b>",
        f"• Farklı holder: {esc(c['distinct_holders'])} <i>(proxy, gerçek unique-alıcı değil)</i>",
        f"• Creator payı: {esc(c['creator_holding_pct'])}   Top5: {esc(c['top5_holder_pct'])}   Top10: {esc(c['top10_holder_pct'])}",
        f"• Creator önceki coin sayısı: {esc(c['creator_prior_coins'])}",
        "",
        "📊 <b>Skor Detayı</b>",
        score_text,
        "",
        f"⚠️ <b>Sert veto:</b> {veto_text}",
        "",
        f"🧾 <code>{esc(c['mint'])}</code>",
    ]
    if c.get("pumpfun_url"):
        lines.append(f'🔗 <a href="{c["pumpfun_url"]}">pump.fun</a>'
                      + (f' | <a href="{c["dexscreener_url"]}">DexScreener</a>' if c.get("dexscreener_url") else ""))
    return "\n".join(lines)


def send_telegram_message(text_html):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text_html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
    except Exception as e:
        print(f"[HATA] Telegram mesajı gönderilemedi: {e}")


def send_telegram_photo(photo_url, caption_html):
    """Coin'in pump.fun görselini kısa başlıkla gönderir. Başarılıysa True döner."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not photo_url:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": photo_url,
            "caption": caption_html[:1024],  # Telegram caption limiti
            "parse_mode": "HTML",
        }, timeout=15)
        return resp.status_code == 200 and resp.json().get("ok", False)
    except Exception as e:
        print(f"[HATA] Telegram fotoğrafı gönderilemedi: {e}")
        return False


def send_telegram_notification(c):
    """Görsel varsa: fotoğraf + kısa başlık, ardından ayrı mesajla tam detay.
    Görsel yoksa veya gönderilemezse: hepsi tek HTML mesajda (eski davranışa
    dönüş, veri kaybı olmaz)."""
    caption = format_telegram_caption(c)
    detail = format_telegram_detail(c)
    photo_sent = send_telegram_photo(c.get("image_uri"), caption)
    if photo_sent:
        send_telegram_message(detail)
    else:
        send_telegram_message(caption + "\n\n" + detail)


def log_geç(symbol, score, veto_count, top_reason):
    print(f"[GEÇ] {symbol} | skor={score}/100 | veto_sayısı={veto_count} | ilk_sebep: {top_reason}")
    # Log yazma hızını yumuşatıyoruz; ard arda çok fazla aday bulunursa
    # Railway'in 500 log/sn limitini aşmayalım diye.
    time.sleep(0.02)


def finalize_or_watch(result, coin, pending, seen):
    """Tam (RugCheck dahil) bir değerlendirme sonucunu işler:
      - sert veto varsa veya skor yetersizse -> GEÇ, kalıcı olarak bitir (seen).
      - temiz + skor yeterliyse -> 'clean_watch' aşamasına al, momentum
        onayı için RECHECK_DELAY_MINUTES sonra tekrar bakılacak.
    Bu, tek bir anlık fotoğrafın yanıltıcı olabileceği (kağıt üstünde temiz
    ama aslında ilgi görmeyip sessizce ölen) durumları elemek içindir.
    'coin' (ham pump.fun coin objesi) clean_watch aşamasına saklanıyor ki
    2. bakışta tekrar değerlendirebilelim (pump.fun'ın tekil coin API'si
    artık JWT gerektirdiği için elimizdeki tek tam veri budur)."""
    mint = coin.get("mint")
    if result["hard_vetoes"]:
        log_geç(result["symbol"], result["score"], len(result["hard_vetoes"]), result["hard_vetoes"][0])
        seen.add(mint)
        mark_seen(mint)
    elif result["score"] >= WATCH_SCORE_THRESHOLD:
        pending[mint] = {
            "stage": "clean_watch",
            "symbol": result["symbol"], "name": result["name"],
            "first_seen_ts": time.time(),
            "first_mcap": result["mcap"],
            "first_holders": result.get("distinct_holders_count"),
            "first_score": result["score"],
            "coin": coin,
        }
        print(f"[BEKLEMEDE] {result['symbol']} | ilk skor={result['score']}/100 | "
              f"{RECHECK_DELAY_MINUTES} dk sonra momentum kontrolü yapılacak")
    else:
        log_geç(result["symbol"], result["score"], len(result["hard_vetoes"]), "skor yetersiz")
        seen.add(mint)
        mark_seen(mint)


def process_young_watch(pending, seen, coins_by_mint=None):
    """'young' aşamasındaki (henüz MIN_AGE_MINUTES'i doldurmamış, bu yüzden
    RugCheck sorgulanmadan sadece not edilmiş) adayları kontrol eder. Yaş
    artık yeterliyse, coin ilk görüldüğünde saklanan ham coin objesiyle
    (info['coin']) tam değerlendirme yapılır (RugCheck dahil). pump.fun'ın
    tekil /coins/{mint} rotası artık JWT (oturum açmış hesap) gerektirdiği
    için o rotayı KULLANMIYORUZ; bunun yerine mcap'i DexScreener'ın herkese
    açık API'sinden tazeliyoruz (bkz. fetch_dexscreener_market_data). Coin
    bu turun en-yeni-50 listesinde hâlâ varsa (coins_by_mint) o taze veri
    öncelikli kullanılır."""
    coins_by_mint = coins_by_mint or {}
    matured = 0
    for mint in list(pending.keys()):
        info = pending[mint]
        if info.get("stage") != "young":
            continue

        age = age_minutes(info.get("created_timestamp"))
        if age is None or age > MAX_AGE_MINUTES:
            # Zamanı geçmiş / veri bozuk -> sessizce bitir, hiç GEÇ logu
            # basmaya değmez (zaten hiç gösterilmemişti).
            del pending[mint]
            seen.add(mint)
            mark_seen(mint)
            continue
        if age < MIN_AGE_MINUTES:
            continue  # sırası henüz gelmedi, listede kalsın

        stored_coin = info.get("coin")
        if stored_coin is None:
            # Eski format bir pending kaydı (coin objesi hiç saklanmamış) ->
            # elimizde artık yeniden üretecek veri yok, sessizce eleriz.
            del pending[mint]
            log_geç(info.get("symbol", "?"), "-", 1, "coin verisi saklanmamış (eski format kaydı)")
            seen.add(mint)
            mark_seen(mint)
            continue

        fresh_coin = dict(coins_by_mint.get(mint, stored_coin))
        time.sleep(0.2)  # DexScreener'a ard arda hızlı istek atmamak için
        fresh_mcap = fetch_dexscreener_market_data(mint)
        if fresh_mcap is not None:
            fresh_coin["usd_market_cap"] = fresh_mcap
            fresh_coin["market_cap"] = fresh_mcap
        # fresh_mcap None ise: coin muhtemelen henüz DexScreener'da hiç
        # pair/indeks oluşturmamış kadar yeni -> ilk görüldüğü andaki mcap
        # ile devam ediyoruz (biraz bayat olabilir ama veri kaybından iyi).

        del pending[mint]  # young aşaması bitti, ya finalize ya da clean_watch'a geçecek
        result = evaluate_candidate(fresh_coin)
        if result is None:
            log_geç(info.get("symbol", "?"), "-", 1, "olgunlaştığında yaş penceresi dışına çıktı")
            seen.add(mint)
            mark_seen(mint)
            continue

        matured += 1
        finalize_or_watch(result, fresh_coin, pending, seen)

    return matured


def process_clean_watch_rechecks(pending, coins_by_mint=None):
    """'clean_watch' aşamasındaki (ilk tam bakışta temiz + skor yeterli çıkmış)
    adayları, süresi dolduysa ikinci kez (momentum kontrolüyle) değerlendirir.
    Onaylanan (gerçekten büyüyen) adayların listesini döner. pump.fun'ın
    tekil coin API'si artık JWT gerektirdiği için ilk görüldüğünde saklanan
    coin objesi (info['coin']) + DexScreener'dan tazelenen mcap kullanılır;
    coin bu turun en-yeni-50 listesinde hâlâ varsa (nadir, çünkü bu aşamadaki
    coin'ler zaten birkaç dakikalık) o taze veri öncelikli kullanılır."""
    coins_by_mint = coins_by_mint or {}
    now_ts = time.time()
    finalized = []
    for mint in list(pending.keys()):
        info = pending[mint]
        if info.get("stage") != "clean_watch":
            continue
        elapsed_min = (now_ts - info["first_seen_ts"]) / 60
        if elapsed_min < RECHECK_DELAY_MINUTES:
            continue  # henüz sırası gelmedi

        stored_coin = info.get("coin")
        if stored_coin is None:
            log_geç(info.get("symbol", "?"), "-", 1, "coin verisi saklanmamış (eski format kaydı)")
            del pending[mint]
            continue

        fresh_coin = dict(coins_by_mint.get(mint, stored_coin))
        time.sleep(0.2)  # DexScreener'a ard arda hızlı istek atmamak için
        fresh_mcap = fetch_dexscreener_market_data(mint)
        if fresh_mcap is not None:
            fresh_coin["usd_market_cap"] = fresh_mcap
            fresh_coin["market_cap"] = fresh_mcap

        result = evaluate_candidate(fresh_coin)
        if result is None:
            log_geç(info.get("symbol", "?"), "-", 1, "ikinci bakışta yaş penceresi dışına çıktı")
            del pending[mint]
            continue

        first_mcap = info.get("first_mcap") or 0
        first_holders = info.get("first_holders")
        new_holders = result.get("distinct_holders_count")
        mcap_growth_pct = ((result["mcap"] - first_mcap) / first_mcap * 100) if first_mcap else 0
        holder_growth = (
            (new_holders - first_holders)
            if (new_holders is not None and first_holders is not None)
            else None
        )

        momentum_note = (
            f"mcap %{mcap_growth_pct:+.0f} (${first_mcap:.0f} -> ${result['mcap']:.0f}), "
            f"holder {('%+d' % holder_growth) if holder_growth is not None else 'doğrulanamadı'} "
            f"({first_holders} -> {new_holders})"
        )
        result["momentum_note"] = momentum_note

        if result["hard_vetoes"]:
            result["decision"] = "GEÇ"
            log_geç(result["symbol"], result["score"], len(result["hard_vetoes"]), result["hard_vetoes"][0])
        elif mcap_growth_pct >= MIN_MCAP_GROWTH_PCT and (holder_growth is not None and holder_growth >= MIN_HOLDER_GROWTH):
            result["decision"] = "İZLE (manuel doğrulama şart, momentum onaylı)"
            finalized.append(result)
        else:
            result["hard_vetoes"] = result["hard_vetoes"] or []
            result["hard_vetoes"].append(f"momentum yetersiz ({momentum_note})")
            result["decision"] = "GEÇ"
            log_geç(result["symbol"], result["score"], len(result["hard_vetoes"]), result["hard_vetoes"][-1])

        del pending[mint]

    return finalized


def run_once(seen, pending):
    # Listeyi en başta çekiyoruz ki AŞAMA A/B'deki adaylar, bu turun en-yeni-50
    # listesinde hâlâ varsa (nadir ama olası) DexScreener'a gitmeden önce en
    # taze pump.fun verisini kullanabilsin.
    coins = fetch_pumpfun_new_coins()
    coins_by_mint = {c.get("mint"): c for c in coins if c.get("mint")}

    # --- AŞAMA A: 'young' (ham, henüz sorgulanmamış) adaylardan olgunlaşanları
    # tam değerlendir (RugCheck dahil) ---
    matured = process_young_watch(pending, seen, coins_by_mint)

    # --- AŞAMA B: 'clean_watch' (ilk bakışta temiz) adaylardan süresi dolanları
    # momentum onayıyla kesinleştir ---
    confirmed = process_clean_watch_rechecks(pending, coins_by_mint)
    for c in confirmed:
        msg = format_message(c)
        print(msg)
        print("-" * 50)
        send_telegram_notification(c)

    # --- AŞAMA C: pump.fun'ın en-yeni-50 listesini tara ---
    # Burada RugCheck/creator-history gibi PAHALI sorgular YAPILMIYOR eğer
    # coin henüz MIN_AGE_MINUTES'i doldurmadıysa - sadece ucuza not ediliyor
    # (mint + oluşturulma zamanı) ve olgunlaştığında AŞAMA A onu bulacak.
    # Bu, pump.fun'da saniyede birden fazla coin çıkması yüzünden "en yeni 50"
    # listesinin çok hızlı akıp bir coin'i 2 dakika dolmadan listeden
    # düşürmesi sorununu çözüyor: artık listede kalmasına bağımlı değiliz,
    # coin'i mint adresinden tekil olarak tekrar çekiyoruz.
    young_added = 0
    matured_on_first_sight = 0
    for coin in coins:
        mint = coin.get("mint")
        if not mint or mint in seen or mint in pending:
            continue

        age = age_minutes(coin.get("created_timestamp"))
        if age is None or age > MAX_AGE_MINUTES:
            continue  # ilgi alanımızın tamamen dışında, dokunma

        if age < MIN_AGE_MINUTES:
            if not has_metadata(coin):
                # Bariz boş/otomatik lansman (isim/görsel/açıklama eksik) ->
                # zamanla düzelmez, izlemeye bile almaya değmez.
                seen.add(mint)
                mark_seen(mint)
                continue
            pending[mint] = {
                "stage": "young",
                "symbol": coin.get("symbol", "?"), "name": coin.get("name", "?"),
                "created_timestamp": coin.get("created_timestamp"),
                "coin": coin,
            }
            young_added += 1
            continue

        # Nadir durum: coin ilk kez görüldüğünde zaten 2+ dk yaşında
        # (poll aralığı büyükse olabilir) -> beklemeye gerek yok, direkt
        # tam değerlendir.
        result = evaluate_candidate(coin)
        if result is None:
            continue
        matured_on_first_sight += 1
        finalize_or_watch(result, coin, pending, seen)

    stage_counts = Counter(info.get("stage") for info in pending.values())
    print(f"[{datetime.now().strftime('%H:%M:%S')}] tarandı={len(coins)} | "
          f"yeni_genç={young_added} | olgunlaşan={matured + matured_on_first_sight} | "
          f"onaylanan_izle={len(confirmed)} | beklemede(genç={stage_counts.get('young', 0)}, "
          f"momentum={stage_counts.get('clean_watch', 0)})")

    save_pending(pending)


def main():
    print("DESK Scanner v6 (üç aşamalı: genç takip -> tam değerlendirme -> momentum onayı) başladı. Ctrl+C ile durdur.")
    seen = load_seen()
    pending = load_pending()
    consecutive_errors = 0
    while True:
        try:
            run_once(seen, pending)
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
