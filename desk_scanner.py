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

# --- TEKRAR DENEME (GOAT dersi) ---
# ESKİ DAVRANIŞ: bir coin MIN_AGE_MINUTES'i dolduğunda TEK BİR kez tam
# değerlendiriliyordu; mcap/holder sayısı o an yetersizse KALICI olarak
# "GEÇ" deniyor ve bir daha ASLA bakılmıyordu. Ama mcap/holder gibi
# kriterler zamanla organik olarak düzelebilir (örn. bir coin 2 dk'da
# $8K iken 10 dk'da $36K olabilir) — bunu kaçırıyorduk. Şimdi: sadece
# GÜVENLİK/KALICI nedenlerle (mint/freeze authority açık, rugged, kopya
# isim, seri launcher, RugCheck 'danger' riski, curve zaten ilerlemiş)
# elenen coinler kalıcı olarak bırakılıyor; SADECE mcap/holder/skor gibi
# İYİLEŞEBİLİR nedenlerle geçemeyenler MAX_AGE_MINUTES'e kadar bu
# aralıkla TEKRAR TEKRAR deneniyor.
YOUNG_RETRY_INTERVAL_MINUTES = 2

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

# --- "Runner" hız eşiği ---
# Dakika başına bu kadar (veya daha fazla) mcap büyüme oranı "anormal hızlı"
# sayılır ve "🔥 HIZLI YÜKSELİŞ" etiketiyle işaretlenir. Bu SADECE bir dikkat-
# çekme etiketidir, güvenlik ya da kazanç GARANTİSİ DEĞİLDİR — en hızlı yükselen
# coinler hem gerçek bir "runner" hem de bir pump-and-dump'ın en şiddetli anı
# olabilir; ikisi ilk bakışta ayırt edilemez.
RUNNER_MCAP_GROWTH_RATE_PCT_PER_MIN = 20

# --- Dev/creator satışı tespiti (2. bakışta) ---
# Sadece creator'ın holder-listesindeki YÜZDESİNE bakmak yanıltıcı olabilir:
# başkaları alım yaptıkça yüzde, creator hiç satmasa bile "dilution" ile
# düşer. Bu yüzden GERÇEK token miktarını (mutlak sayı) karşılaştırıyoruz.
# İlk bakışla 2. bakış arasında creator'ın elindeki miktar bu yüzdeden fazla
# azalmışsa (0'a düşmesi dahil) -> "dev satmış" sayılır ve SERT VETO uygulanır.
DEV_SELL_DROP_THRESHOLD_PCT = 50

# --- İZLE SONRASI TAKİP (RETARDO dersi) ---
# Bir coin İZLE onayı aldıktan HEMEN SONRA da dev satabilir — momentum
# kontrolü sadece TEK bir ek bakıştı, sonsuza kadar güvenlik garantisi
# vermiyordu. Bu yüzden İZLE sinyali gönderildikten sonra da coin'i bir
# süre daha (arka planda, sessizce) izlemeye devam ediyoruz; dev elindeki
# tokenin büyük kısmını satarsa AYRI ve ACİL bir "⚠️ DEV SATTI" uyarısı
# gönderiyoruz. Bu, ilk İZLE mesajını geri almaz ama kullanıcıyı anında
# haberdar eder.
POST_CONFIRM_WATCH_MINUTES = 45        # İZLE'den sonra bu kadar dakika daha izlenir
POST_CONFIRM_CHECK_INTERVAL_MINUTES = 5  # bu aralıklarla dev cüzdanı tekrar kontrol edilir

# --- KALICI DEPOLAMA (Railway redeploy sorunu) ---
# ÖNEMLİ: Railway (ve genelde container platformları) her yeni deploy'da
# konteyneri SIFIRDAN oluşturur — konteyner içine yazılan dosyalar (bu
# script'in seen/pending/top10 dosyaları dahil) kalıcı bir Volume
# BAĞLANMADIĞI sürece her deploy'da tamamen SİLİNİR. Bir coin'in İZLE'ye
# ulaşması en az ~7 dakika sürdüğü için (2 dk young + 5 dk momentum
# bekleme), sık sık deploy edildiğinde hiçbir aday bu süreyi tamamlayamadan
# ilerleme kayboluyor ve hep sıfırdan başlanıyor. Çözüm: Railway'de bir
# Volume oluşturup DESK_DATA_DIR ortam değişkenini o volume'ün mount
# path'ine ayarla (örn. /data). Ayarlanmazsa eskisi gibi konteynerin kendi
# (kalıcı olmayan) dizininde çalışmaya devam eder — kod ÇALIŞMAYA devam
# eder, sadece deploy'lar arası hafıza kalıcı olmaz.
DATA_DIR = os.environ.get("DESK_DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

SEEN_FILE = os.path.join(DATA_DIR, "desk_seen_tokens.txt")
PENDING_FILE = os.path.join(DATA_DIR, "desk_pending_recheck.json")   # ilk bakışta temiz çıkıp 2. bakış bekleyen adaylar

# --- MC SIRALI TOP 10 YAYINI (butona basınca detay) ---
# Her 5 dakikada bir, o an taranan 50 coin'in market cap'e göre en yüksek
# 10 tanesi TEK bir Telegram mesajında, her biri ayrı bir buton olarak
# listeleniyor. Bu, evaluate_candidate/RugCheck gibi PAHALI bir işlem
# DEĞİL — sadece o anki mcap'e göre bir "kısa liste". Butona basılınca
# (Telegram inline keyboard callback) o coin için TAM değerlendirme
# (RugCheck dahil) o an yapılıp ayrı bir mesaj olarak gönderiliyor.
TOP10_BROADCAST_INTERVAL_MINUTES = 5
TOP10_CACHE_FILE = os.path.join(DATA_DIR, "desk_top10_cache.json")     # butona basılınca hangi coin'e karşılık geldiğini bilmek için
TOP10_CACHE_TTL_MINUTES = 120                  # önbellekte tutulma süresi (bundan eski coin'ler için buton artık çalışmaz)
TOP10_STATE_FILE = os.path.join(DATA_DIR, "desk_top10_state.json")     # son yayın zamanı + Telegram update offset

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


def load_top10_cache():
    """Top10 butonlarının hangi mint'e karşılık geldiğini hatırlamak için."""
    if not os.path.exists(TOP10_CACHE_FILE):
        return {}
    try:
        with open(TOP10_CACHE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_top10_cache(cache):
    try:
        with open(TOP10_CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        print(f"[HATA] Top10 önbelleği kaydedilemedi: {e}")


def prune_top10_cache(cache):
    """Eski (artık tıklanamayacak kadar bayat) önbellek kayıtlarını temizler."""
    now_ts = time.time()
    for mint in list(cache.keys()):
        cached_ts = cache[mint].get("cached_ts", 0)
        if (now_ts - cached_ts) / 60 > TOP10_CACHE_TTL_MINUTES:
            del cache[mint]


def load_top10_state():
    """Son yayın zamanı ve Telegram getUpdates offset'i (hangi güncellemelere
    kadar okunduğu — aynı buton tıklamasını tekrar tekrar işlememek için)."""
    if not os.path.exists(TOP10_STATE_FILE):
        return {}
    try:
        with open(TOP10_STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_top10_state(state):
    try:
        with open(TOP10_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[HATA] Top10 durumu kaydedilemedi: {e}")


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
    fetch_dexscreener_data()/apply_dexscreener_data() (kimlik gerektirmeyen public API)
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


def fetch_dexscreener_data(mint):
    """DexScreener'ın herkese açık, kimlik/JWT gerektirmeyen token-pairs
    API'sinden coin'in TAZE market cap'ini + 5 dakikalık hacim/işlem
    sayısını çeker. pump.fun'ın kendi tekil coin endpoint'i artık login
    gerektirdiği için (bkz. yukarıdaki not) bu tek güvenilir 'ikinci bakış'
    veri kaynağımız. Hacim/alım-satım sayısı, 'runner' tespiti için ekstra
    bir sinyal — sadece mcap büyümesi değil, KAÇ FARKLI işlemle büyüdüğünü
    de görmek istiyoruz (tek dev bir alım ile organik ilgi arasındaki fark).
    Coin çok yeniyse (henüz hiç DEX/pump.fun pair'i indekslenmediyse) None
    dönebilir — çağıran kod bu durumda ilk görüldüğü andaki veriyle devam
    eder (tamamen veri kaybetmektense biraz bayat veri daha iyidir)."""
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
        volume = best.get("volume") or {}
        txns_m5 = (best.get("txns") or {}).get("m5") or {}
        return {
            "mcap": float(mcap) if mcap is not None else None,
            "volume_m5": volume.get("m5"),
            "buys_m5": txns_m5.get("buys"),
            "sells_m5": txns_m5.get("sells"),
        }
    except Exception as e:
        print(f"[HATA] DexScreener market verisi çekilemedi (mint={mint}): {type(e).__name__}: {e}")
        return None


def apply_dexscreener_data(coin, mint):
    """fetch_dexscreener_data'yı çağırır ve sonucu (varsa) coin objesinin
    üzerine yazar (mcap + hacim/işlem alanları). Tüm çağıran yerlerde aynı
    5 satırı tekrarlamamak için tek yerden yönetiliyor."""
    dex_data = fetch_dexscreener_data(mint)
    if dex_data:
        if dex_data.get("mcap") is not None:
            coin["usd_market_cap"] = dex_data["mcap"]
            coin["market_cap"] = dex_data["mcap"]
        coin["_dex_volume_m5"] = dex_data.get("volume_m5")
        coin["_dex_buys_m5"] = dex_data.get("buys_m5")
        coin["_dex_sells_m5"] = dex_data.get("sells_m5")
    return coin


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


def get_holder_token_amount(h):
    """Bir holder objesinden GERÇEK (ondalık düzeltilmiş) token miktarını
    çıkarır. RugCheck bazen hazır 'uiAmount' veriyor, bazen sadece ham
    'amount' + 'decimals' — ikisini de destekliyoruz. Bu, sadece YÜZDE değil
    creator'ın GERÇEKTEN elinde kaç token tuttuğunu zaman içinde
    karşılaştırabilmek (dev satmış mı tespiti) için gerekli."""
    ui_amount = h.get("uiAmount")
    if ui_amount is not None:
        try:
            return float(ui_amount)
        except (TypeError, ValueError):
            pass
    amount = h.get("amount")
    decimals = h.get("decimals")
    if amount is not None and decimals is not None:
        try:
            return float(amount) / (10 ** int(decimals))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return None


def get_creator_holding_amount(holders, creator):
    """Creator'ın topHolders listesindeki GERÇEK token miktarı (yüzde değil,
    mutlak sayı). Bu, momentum kontrolünde ilk bakışla ikinci bakış arasında
    creator'ın elindeki miktarın GERÇEKTEN azalıp azalmadığını (dev satmış
    mı) anlamak için kullanılıyor — sadece % değişimine bakmak yanıltıcı
    olabilir, çünkü başkaları alım yaptıkça creator'ın YÜZDESİ, elindeki
    miktar hiç değişmeden de düşebilir (dilution). Listede hiç yoksa 0.0
    döner (o an elinde token kalmamış demektir)."""
    if holders is None or not creator:
        return None
    total = 0.0
    for h in holders:
        if h.get("address") == creator or h.get("owner") == creator:
            amt = get_holder_token_amount(h)
            if amt is not None:
                total += amt
    return total


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


# --------------------------- VETO SINIFLANDIRMASI (GOAT dersi) ---------------------------
# Bu metinlerden biri bir veto sebebinde GEÇERSE, o veto "KALICI/GÜVENLİK"
# sayılır (bir daha asla düzelmeyecek/riskli bir durum) -> coin tamamen
# elenir. Listede OLMAYAN vetolar (mcap düşük, holder az, top5/top10 %
# yüksek, skor yetersiz, RugCheck verisi geçici alınamadı vb.) "İYİLEŞEBİLİR"
# sayılır -> coin MAX_AGE_MINUTES'e kadar tekrar tekrar denenir.
PERMANENT_VETO_MARKERS = (
    "metadata eksik",
    "ismine/temasına aşırı benziyor",       # kopya/tuzak isim, değişmez
    "seri/spam launcher paterni",            # creator geçmişi, değişmez
    "'rugged' olarak işaretlemiş",           # RugCheck kesin rug demiş
    "mint authority hâlâ aktif",             # gerçek güvenlik riski
    "freeze authority hâlâ aktif",           # gerçek güvenlik riski
    "RugCheck risk skoru çok yüksek",        # genelde kod-seviyeli/yapısal risk
    "'danger' riskleri",                     # RugCheck'in en ciddi bulgu seviyesi
    "bonding curve ~%",                      # zaten mezuniyete yakın, "erken fırsat" tezimize artık uymuyor
)


def is_permanent_veto(veto_text):
    return any(marker in veto_text for marker in PERMANENT_VETO_MARKERS)


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
        creator_pct = None
        creator_amount = None
        creator_holds_display = "doğrulanamadı"
    else:
        mint_auth, freeze_auth = extract_authorities(report)
        holders = get_non_escrow_holders(report, coin)
        top5pct = sum_pct(holders, 5)
        top10pct = sum_pct(holders, 10)
        creator_pct = get_creator_holding_pct(holders, creator)
        creator_amount = get_creator_holding_amount(holders, creator)
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
        if creator_amount is None:
            creator_holds_display = "doğrulanamadı"
        elif creator_amount > 0:
            creator_holds_display = f"EVET, tutuyor ({creator_amount:.0f} token, {creator_pct_display})"
        else:
            creator_holds_display = "HAYIR, elinde hiç token yok (satmış veya hiç almamış)"

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

    # --- Alım/satım baskısı (DexScreener, 5 dk) — "runner" tespiti için ek sinyal ---
    # Sadece mcap büyümesine değil, KAÇ FARKLI işlemle büyüdüğüne de bakıyoruz:
    # çoğunluğu alım olan bir işlem akışı organik ilgiye işaret eder; ağır
    # satış baskısı altında büyüyen bir mcap ise (nadir ama olur) şüphelidir.
    # Veri yoksa (coin DexScreener'da henüz hiç pair oluşturmadıysa) veto
    # UYGULANMAZ — bu, henüz çok yeni bir coin için normaldir.
    buys_m5 = coin.get("_dex_buys_m5")
    sells_m5 = coin.get("_dex_sells_m5")
    volume_m5 = coin.get("_dex_volume_m5")
    if buys_m5 is not None and sells_m5 is not None and (buys_m5 + sells_m5) > 0:
        buy_ratio = buys_m5 / (buys_m5 + sells_m5)
        volume_display = (
            f"{buys_m5} alım / {sells_m5} satım (5dk)"
            + (f", hacim ${volume_m5:,.0f}" if volume_m5 is not None else "")
        )
        if buy_ratio >= 0.65:
            score += 10
            score_notes.append(f"+10 alım baskısı güçlü (5dk: %{buy_ratio*100:.0f} alım, {buys_m5}/{sells_m5+buys_m5})")
        elif buy_ratio <= 0.35:
            score -= 10
            score_notes.append(f"-10 satış baskısı ağır basıyor (5dk: %{(1-buy_ratio)*100:.0f} satım)")
    else:
        volume_display = "doğrulanamadı (DexScreener'da henüz pair yok/çok yeni)"

    score = max(0.0, min(100.0, score))
    # NOT: has_permanent_veto skor-yetersizliği vetosu EKLENMEDEN önce
    # hesaplanıyor çünkü "skor yetersiz" kendi başına her zaman İYİLEŞEBİLİR
    # sayılır (mcap/holder büyüdükçe skor da büyür) — kalıcı listede yok zaten.
    has_permanent_veto = any(is_permanent_veto(v) for v in hard_vetoes)

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
        "creator_holding_pct_value": creator_pct,      # sayısal (karşılaştırma için)
        "creator_holding_amount": creator_amount,      # GERÇEK token miktarı (dev satışı tespiti için)
        "creator_holds": creator_holds_display,        # "şu an dev'in elinde coin var mı" (evet/hayır/doğrulanamadı)
        "top5_holder_pct": top5_display, "top10_holder_pct": top10_display,
        "rugcheck_risk_score": rc_score_display,
        "score": round(score, 1), "score_notes": score_notes,
        "hard_vetoes": hard_vetoes, "decision": decision,
        "has_permanent_veto": has_permanent_veto,
        "dex_volume_info": volume_display,          # "X alım / Y satım (5dk), hacim $Z"
        "dex_buys_m5": buys_m5, "dex_sells_m5": sells_m5, "dex_volume_m5": volume_m5,
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
        f"- Alım/satım (5dk, DexScreener): {c.get('dex_volume_info', 'doğrulanamadı')}",
        f"- Farklı holder sayısı (proxy, gerçek unique-alıcı değil): {c['distinct_holders']}",
        f"- Dev/creator şu an token tutuyor mu: {c.get('creator_holds', 'doğrulanamadı')}",
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
    is_fast_riser = bool(c.get("is_fast_riser"))
    emoji = ("🔥" if (is_watch and is_fast_riser) else "🟢") if is_watch else "🔴"
    fast_riser_line = (
        "\n⚡ <b>HIZLI YÜKSELİŞ</b> — dikkat çekici ama bu bir garanti DEĞİL "
        "(gerçek runner ya da pump-dump'ın zirvesi olabilir, ikisi aynı görünür)"
        if (is_watch and is_fast_riser) else ""
    )
    return (
        f"{emoji} <b>{esc(c['symbol'])}</b>  (<i>{esc(c['name'])}</i>)\n"
        f"💯 <b>{c['score']}/100</b>  —  {esc(c['decision'])}\n"
        f"⏱ {c['age_min']} dk   💰 ${c['mcap']:.0f}   📈 curve ~%{c['curve_pct']}"
        f"{fast_riser_line}"
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
        "📊 <b>Alım/Satım (5dk, DexScreener)</b>",
        f"• {esc(c.get('dex_volume_info', 'doğrulanamadı'))}",
        "",
        "👥 <b>Holder Dağılımı</b>",
        f"• Farklı holder: {esc(c['distinct_holders'])} <i>(proxy, gerçek unique-alıcı değil)</i>",
        f"• 🕵️ Dev/creator şu an token tutuyor mu: {esc(c.get('creator_holds', 'doğrulanamadı'))}",
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


def send_telegram_message_with_keyboard(text_html, keyboard_rows):
    """sendMessage + inline keyboard (her satır bir buton listesi). Butona
    basılınca Telegram bize bir 'callback_query' güncellemesi gönderir
    (bkz. fetch_telegram_updates / process_telegram_callbacks)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text_html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": json.dumps({"inline_keyboard": keyboard_rows}),
        }, timeout=10)
    except Exception as e:
        print(f"[HATA] Telegram klavyeli mesajı gönderilemedi: {e}")


def fetch_telegram_updates(offset):
    """Telegram'a 'uzun polling' ile son güncellemeleri (buton tıklamaları
    dahil) sorar. Bot'un webhook'u YOK — periyodik olarak bunu çağırıyoruz,
    ayrı bir web sunucusu/URL gerekmiyor."""
    if not TELEGRAM_BOT_TOKEN:
        return []
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        resp = requests.get(url, params={
            "offset": offset,
            "timeout": 0,
            "allowed_updates": json.dumps(["callback_query"]),
        }, timeout=10)
        if resp.status_code != 200:
            return []
        return resp.json().get("result", [])
    except Exception as e:
        print(f"[HATA] Telegram güncellemeleri alınamadı: {e}")
        return []


def answer_callback_query(callback_query_id, text=None):
    """Butona basınca Telegram'ın gösterdiği 'yükleniyor' animasyonunu
    kapatır ve istersek küçük bir bildirim (toast) gösterir."""
    if not TELEGRAM_BOT_TOKEN or not callback_query_id:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
        data = {"callback_query_id": callback_query_id}
        if text:
            data["text"] = text[:200]
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print(f"[HATA] Telegram callback yanıtlanamadı: {e}")


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


def send_early_watch_notification(c):
    """'clean_watch' aşamasına GİRER GİRMEZ (momentum onayı BEKLENMEDEN)
    gönderilen ERKEN, henüz KESİNLEŞMEMİŞ sinyal. Amaç: kullanıcı 5 dakika
    boyunca hiçbir şey duymamak yerine, ilk bakışta temiz + skoru yeterli
    çıkan adayları hemen görebilsin — ama bunun kesin bir İZLE olmadığı,
    momentum testinin hâlâ önünde olduğu açıkça belirtiliyor. DESK asla
    kesinmiş gibi konuşmaz (bkz. MUTLAK KURALLAR); bu yüzden ayrı ve daha
    temkinli bir metin kullanıyoruz, format_telegram_caption'daki normal
    İZLE metnini DEĞİL."""
    caption = (
        f"🔍 <b>{esc(c['symbol'])}</b>  (<i>{esc(c['name'])}</i>)  —  <b>ERKEN SİNYAL</b>\n"
        f"💯 <b>{c['score']}/100</b>  —  ilk bakışta temiz, momentum onayı BEKLENİYOR\n"
        f"⏱ {c['age_min']} dk   💰 ${c['mcap']:.0f}   📈 curve ~%{c['curve_pct']}\n\n"
        f"⚠️ Bu KESİN bir İZLE değil — {RECHECK_DELAY_MINUTES} dakika sonra gerçekten "
        f"büyüyor mu (mcap/holder) diye ayrıca kontrol edilecek. Sonucu (onaylandı ya "
        f"da onaylanamadı) ayrı bir mesajla bildireceğiz."
    )
    detail = format_telegram_detail(c)
    photo_sent = send_telegram_photo(c.get("image_uri"), caption)
    if photo_sent:
        send_telegram_message(detail)
    else:
        send_telegram_message(caption + "\n\n" + detail)


def send_early_watch_retraction(symbol, name, momentum_note):
    """Erken sinyal gönderilmiş bir adayın momentum testini GEÇEMEDİĞİ
    durumda gönderilen kısa 'geri çekme' notu — kullanıcı erken sinyali
    gördükten sonra sonucun ne olduğunu merak etmesin diye."""
    text = (
        f"❌ <b>{esc(symbol)}</b> (<i>{esc(name)}</i>) için gönderdiğimiz erken sinyal "
        f"momentum testini GEÇEMEDİ — artık İZLE değil, GEÇ.\n\n"
        f"{esc(momentum_note)}"
    )
    send_telegram_message(text)


def send_top10_by_mcap(coins, top10_cache):
    """O anki (en yeni 50) taramadan, market cap'e göre en yüksek 10 coin'i
    TEK bir mesajda, her biri kendi butonuyla listeler. Bu adım RugCheck/
    DexScreener gibi PAHALI bir şey YAPMAZ — sadece pump.fun listesinden
    zaten gelen mcap'e göre basit bir sıralama. Tam değerlendirme (RugCheck
    dahil) sadece kullanıcı bir butona BASARSA o coin için yapılır (bkz.
    process_telegram_callbacks) — böylece 50 coin'in hepsini boşuna
    sorgulamamış oluyoruz."""
    scored = []
    for coin in coins:
        mint = coin.get("mint")
        if not mint:
            continue
        mcap = coin.get("usd_market_cap") or coin.get("market_cap") or 0
        scored.append((mcap, coin))
    scored.sort(key=lambda x: x[0], reverse=True)
    top10 = scored[:10]
    if not top10:
        return

    lines = ["📊 <b>Market Cap Sıralı İlk 10</b>  <i>(son taranan 50 coin içinden)</i>", ""]
    keyboard_rows = []
    for i, (mcap, coin) in enumerate(top10, start=1):
        mint = coin["mint"]
        symbol = coin.get("symbol", "?")
        lines.append(f"{i}. ${esc(symbol)} — ${mcap:,.0f}")
        keyboard_rows.append([{"text": f"${symbol}", "callback_data": f"info:{mint}"}])
        top10_cache[mint] = {
            "coin": coin, "symbol": symbol, "name": coin.get("name", "?"),
            "cached_ts": time.time(),
        }
    lines.append("")
    lines.append("👇 Detaylı (RugCheck dahil) rapor için bir coin'e dokun.")

    send_telegram_message_with_keyboard("\n".join(lines), keyboard_rows)
    print(f"[BİLGİ] Top10 (mcap sıralı) Telegram'a gönderildi: "
          f"{', '.join('$' + c.get('symbol', '?') for _, c in top10)}")


def process_telegram_callbacks(top10_cache, state):
    """Top10 mesajındaki butonlardan birine basılıp basılmadığını kontrol
    eder (Telegram'ın getUpdates'i ile — webhook/ayrı sunucu GEREKMEZ).
    Basılan coin için TAM değerlendirme (RugCheck dahil) o an yapılıp ayrı
    bir mesaj olarak gönderilir."""
    offset = state.get("telegram_update_offset", 0)
    updates = fetch_telegram_updates(offset)
    for upd in updates:
        offset = max(offset, upd.get("update_id", 0) + 1)
        cq = upd.get("callback_query")
        if not cq:
            continue
        cq_id = cq.get("id")
        data = cq.get("data") or ""
        if not data.startswith("info:"):
            answer_callback_query(cq_id)
            continue

        mint = data[len("info:"):]
        entry = top10_cache.get(mint)
        if entry is None:
            answer_callback_query(cq_id, "Bu coin artık önbellekte değil (çok eski/süresi dolmuş).")
            continue

        answer_callback_query(cq_id, "Bilgiler getiriliyor (RugCheck dahil, birkaç saniye sürebilir)...")

        stored_coin = entry.get("coin") or {}
        fresh_coin = dict(stored_coin)
        apply_dexscreener_data(fresh_coin, mint)

        result = evaluate_candidate(fresh_coin)
        if result is None:
            send_telegram_message(
                f"⚠️ <b>${esc(entry.get('symbol', '?'))}</b> artık değerlendirme "
                f"penceresinin dışında (çok yaşlanmış, {MAX_AGE_MINUTES} dk'yı geçmiş)."
            )
        else:
            send_telegram_notification(result)

    state["telegram_update_offset"] = offset


def log_geç(symbol, score, veto_count, top_reason):
    print(f"[GEÇ] {symbol} | skor={score}/100 | veto_sayısı={veto_count} | ilk_sebep: {top_reason}")
    # Log yazma hızını yumuşatıyoruz; ard arda çok fazla aday bulunursa
    # Railway'in 500 log/sn limitini aşmayalım diye.
    time.sleep(0.02)


def send_dev_dump_alert(symbol, name, mint, baseline_amount, new_amount, drop_pct, minutes_since_confirm):
    """İZLE sinyali gönderildikten SONRA dev'in tokeninin büyük kısmını/tamamını
    sattığını tespit edince gönderilen AYRI ve ACİL uyarı. İlk İZLE mesajını
    geri almaz (Telegram'da öyle bir şey yok zaten) ama kullanıcıyı hemen
    haberdar eder — RETARDO'da yaşanan 'alarm sonrası sessiz dump' durumunu
    yakalamak için eklendi."""
    text = (
        f"🚨 <b>DEV SATIŞ UYARISI</b> 🚨\n\n"
        f"<b>{esc(symbol)}</b> (<i>{esc(name)}</i>) için gönderdiğimiz İZLE sinyalinden "
        f"~{minutes_since_confirm:.0f} dakika sonra dev/creator cüzdanının "
        f"elindeki tokenin ~%{drop_pct:.0f}'ini sattığı tespit edildi.\n\n"
        f"İlk bakış: {baseline_amount:.0f} token → şimdi: {new_amount:.0f} token\n\n"
        f"🧾 <code>{esc(mint)}</code>\n"
        f"🔗 <a href=\"https://pump.fun/coin/{mint}\">pump.fun</a> | "
        f"<a href=\"https://dexscreener.com/solana/{mint}\">DexScreener</a>\n\n"
        f"⚠️ Bu, önceki İZLE kararını iptal etmez ama artık ek bir risk sinyali "
        f"var demektir — pozisyon açtıysan (biz açmadık, sadece bilgilendiriyoruz) "
        f"gözden geçir."
    )
    send_telegram_message(text)


def route_first_look_result(result, coin, pending, seen):
    """Bir tam değerlendirme (RugCheck dahil) sonucunu ÜÇ yola ayırır:
      1) KALICI/GÜVENLİK vetosu varsa (bkz. PERMANENT_VETO_MARKERS) -> GEÇ,
         kalıcı olarak bitir (seen). Bir daha asla bakılmaz.
      2) Temiz + skor yeterliyse -> 'clean_watch' aşamasına al, momentum
         onayı için RECHECK_DELAY_MINUTES sonra tekrar bakılacak.
      3) SADECE İYİLEŞEBİLİR nedenlerle (mcap düşük, holder az, skor
         yetersiz vb.) geçemediyse -> KALICI OLARAK ELEMEZ, 'young'
         aşamasına (tekrar denenecek şekilde) geri koyar. GOAT'ın 2 dk'da
         $8K'dan 10 dk'da $36K'a çıkıp da hiç sinyal vermemesi TAM OLARAK bu
         yüzdendi — eskiden her ilk-bakış-başarısızlığı kalıcıydı.
    'coin' (ham pump.fun coin objesi) hem clean_watch hem young'a
    saklanıyor ki sonraki bakışta tekrar değerlendirebilelim (pump.fun'ın
    tekil coin API'si artık JWT gerektirdiği için elimizdeki tek tam veri
    budur)."""
    mint = coin.get("mint")
    if result["hard_vetoes"] and result.get("has_permanent_veto"):
        log_geç(result["symbol"], result["score"], len(result["hard_vetoes"]), result["hard_vetoes"][0])
        seen.add(mint)
        mark_seen(mint)
    elif not result["hard_vetoes"] and result["score"] >= WATCH_SCORE_THRESHOLD:
        pending[mint] = {
            "stage": "clean_watch",
            "symbol": result["symbol"], "name": result["name"],
            "first_seen_ts": time.time(),
            "first_mcap": result["mcap"],
            "first_holders": result.get("distinct_holders_count"),
            "first_score": result["score"],
            "first_creator_amount": result.get("creator_holding_amount"),
            "first_creator_pct_value": result.get("creator_holding_pct_value"),
            "coin": coin,
        }
        print(f"[BEKLEMEDE] {result['symbol']} | ilk skor={result['score']}/100 | "
              f"{RECHECK_DELAY_MINUTES} dk sonra momentum kontrolü yapılacak")
        # Kullanıcı 5 dk boyunca hiçbir şey duymasın diye BEKLEMİYORUZ —
        # ilk bakışta temiz + skoru yeterli çıktığı anda ERKEN (henüz
        # kesinleşmemiş) bir sinyal gönderiyoruz. Sonuç (onaylandı/onaylanamadı)
        # process_clean_watch_rechecks'te ayrıca bildirilecek.
        send_early_watch_notification(result)
    else:
        top_reason = result["hard_vetoes"][0] if result["hard_vetoes"] else "skor yetersiz"
        pending[mint] = {
            "stage": "young",
            "symbol": result["symbol"], "name": result["name"],
            "created_timestamp": coin.get("created_timestamp"),
            "coin": coin,
            "last_full_check_ts": time.time(),
        }
        print(f"[TEKRAR DENENECEK] {result['symbol']} | skor={result['score']}/100 | "
              f"şu anki sebep: {top_reason} | {YOUNG_RETRY_INTERVAL_MINUTES} dk sonra tekrar bakılacak")


def process_young_watch(pending, seen, coins_by_mint=None):
    """'young' aşamasındaki (henüz MIN_AGE_MINUTES'i doldurmamış, bu yüzden
    RugCheck sorgulanmadan sadece not edilmiş) adayları kontrol eder. Yaş
    artık yeterliyse, coin ilk görüldüğünde saklanan ham coin objesiyle
    (info['coin']) tam değerlendirme yapılır (RugCheck dahil). pump.fun'ın
    tekil /coins/{mint} rotası artık JWT (oturum açmış hesap) gerektirdiği
    için o rotayı KULLANMIYORUZ; bunun yerine mcap'i DexScreener'ın herkese
    açık API'sinden tazeliyoruz (bkz. fetch_dexscreener_data/apply_dexscreener_data). Coin
    bu turun en-yeni-50 listesinde hâlâ varsa (coins_by_mint) o taze veri
    öncelikli kullanılır."""
    coins_by_mint = coins_by_mint or {}
    matured = 0
    now_ts = time.time()
    for mint in list(pending.keys()):
        info = pending[mint]
        if info.get("stage") != "young":
            continue

        age = age_minutes(info.get("created_timestamp"))
        if age is None or age > MAX_AGE_MINUTES:
            # Zamanı geçmiş / veri bozuk -> pencere kapandı, hiç geçemedi.
            del pending[mint]
            if age is not None:
                log_geç(info.get("symbol", "?"), "-", 1, "yaş penceresi (MAX_AGE_MINUTES) dolana kadar hiç geçemedi")
            seen.add(mint)
            mark_seen(mint)
            continue
        if age < MIN_AGE_MINUTES:
            continue  # sırası henüz gelmedi, listede kalsın

        # Bu adayı daha önce en az bir kez tam değerlendirdiysek (RugCheck
        # dahil), RugCheck'i her 60sn'lik turda değil YOUNG_RETRY_INTERVAL_MINUTES
        # aralığıyla tekrar sorguluyoruz — hem gereksiz yere yormamak hem de
        # mcap/holder'ın gerçekten değişmesine zaman tanımak için.
        last_check_ts = info.get("last_full_check_ts")
        if last_check_ts is not None and (now_ts - last_check_ts) / 60 < YOUNG_RETRY_INTERVAL_MINUTES:
            continue

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
        apply_dexscreener_data(fresh_coin, mint)
        # DexScreener'dan veri gelmezse: coin muhtemelen henüz orada hiç
        # pair/indeks oluşturmamış kadar yeni -> ilk görüldüğü andaki mcap
        # ile devam ediyoruz (biraz bayat olabilir ama veri kaybından iyi).

        del pending[mint]  # bu turluk 'young' kaydı bitti; route_first_look_result
                            # ya yeniden 'young' (tekrar denenecek) olarak ekleyecek
                            # ya başka bir aşamaya geçirecek ya da kalıcı GEÇ yapacak
        result = evaluate_candidate(fresh_coin)
        if result is None:
            log_geç(info.get("symbol", "?"), "-", 1, "yaş penceresi (MAX_AGE_MINUTES) dolana kadar hiç geçemedi")
            seen.add(mint)
            mark_seen(mint)
            continue

        matured += 1
        route_first_look_result(result, fresh_coin, pending, seen)

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
        apply_dexscreener_data(fresh_coin, mint)

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

        # --- Hız/ivme (runner tespiti) ---
        # Sadece "büyüdü mü" değil, "NE KADAR HIZLI büyüdü" bilgisini de
        # veriyoruz: dakika başına mcap büyüme yüzdesi ve dakika başına
        # eklenen holder sayısı. Bu, tek başına güvenlik garantisi DEĞİL —
        # hem gerçek "runner" hem de pump-and-dump'ın en şiddetli anı aynı
        # şekilde görünebilir. Sadece "buna dikkat et" sinyali.
        mcap_growth_rate_per_min = (mcap_growth_pct / elapsed_min) if elapsed_min > 0 else 0
        holder_growth_rate_per_min = (
            (holder_growth / elapsed_min) if (holder_growth is not None and elapsed_min > 0) else None
        )
        is_fast_riser = mcap_growth_rate_per_min >= RUNNER_MCAP_GROWTH_RATE_PCT_PER_MIN

        momentum_note = (
            f"mcap %{mcap_growth_pct:+.0f} (${first_mcap:.0f} -> ${result['mcap']:.0f}), "
            f"holder {('%+d' % holder_growth) if holder_growth is not None else 'doğrulanamadı'} "
            f"({first_holders} -> {new_holders}) | "
            f"hız: %{mcap_growth_rate_per_min:+.0f}/dk"
            + (f", +{holder_growth_rate_per_min:.1f} holder/dk" if holder_growth_rate_per_min is not None else "")
        )
        if is_fast_riser:
            momentum_note += " | 🔥 HIZLI YÜKSELİŞ (hem gerçek fırsat hem de pump-dump'ın ortası olabilir, dikkatli ol)"
        result["is_fast_riser"] = is_fast_riser

        # --- Dev/creator satışı tespiti: YÜZDE değil GERÇEK miktar karşılaştırılıyor
        # (başkaları alım yaptıkça yüzde, creator hiç satmasa bile dilution'la
        # düşebilir — bu yanıltıcı olur). İlk bakışta creator'ın elinde
        # anlamlı miktarda token varken, 2. bakışta bunun büyük kısmı/tamamı
        # gitmişse -> "dev satmış" sayılır, SERT VETO. ---
        first_creator_amount = info.get("first_creator_amount")
        new_creator_amount = result.get("creator_holding_amount")
        dev_sold_note = None
        if (first_creator_amount is not None and first_creator_amount > 0
                and new_creator_amount is not None):
            drop_pct = (first_creator_amount - new_creator_amount) / first_creator_amount * 100
            if drop_pct >= DEV_SELL_DROP_THRESHOLD_PCT:
                dev_sold_note = (
                    f"creator/dev cüzdanı elindeki tokenin ~%{drop_pct:.0f}'ini satmış "
                    f"(ilk bakış: {first_creator_amount:.0f} token -> 2. bakış: {new_creator_amount:.0f} token)"
                )
                result["hard_vetoes"].append(dev_sold_note)

        if dev_sold_note:
            momentum_note += f" | ⚠️ {dev_sold_note}"
        result["momentum_note"] = momentum_note

        if result["hard_vetoes"]:
            result["decision"] = "GEÇ"
            log_geç(result["symbol"], result["score"], len(result["hard_vetoes"]), result["hard_vetoes"][0])
            # Daha önce erken sinyal göndermiştik (bkz. send_early_watch_notification) —
            # şimdi o sinyalin geçersiz olduğunu bildiriyoruz.
            send_early_watch_retraction(result["symbol"], result["name"], result["hard_vetoes"][0])
        elif mcap_growth_pct >= MIN_MCAP_GROWTH_PCT and (holder_growth is not None and holder_growth >= MIN_HOLDER_GROWTH):
            result["decision"] = "İZLE (manuel doğrulama şart, momentum onaylı)"
            # İZLE'den SONRA da dev cüzdanını izlemeye devam edebilmek için ham
            # coin objesini ve bu andaki creator miktarını saklıyoruz (bkz.
            # process_post_confirm_watch / "İZLE sonrası takip").
            result["_raw_coin"] = fresh_coin
            finalized.append(result)
        else:
            result["hard_vetoes"] = result["hard_vetoes"] or []
            result["hard_vetoes"].append(f"momentum yetersiz ({momentum_note})")
            result["decision"] = "GEÇ"
            log_geç(result["symbol"], result["score"], len(result["hard_vetoes"]), result["hard_vetoes"][-1])
            send_early_watch_retraction(result["symbol"], result["name"], momentum_note)

        del pending[mint]

    return finalized


def process_post_confirm_watch(pending, coins_by_mint=None):
    """İZLE sinyali gönderilmiş adayları, sinyal SONRASINDA da bir süre
    (POST_CONFIRM_WATCH_MINUTES) sessizce izlemeye devam eder. Amaç: dev'in
    tam da alarmı gönderdiğimiz andan hemen sonra satması durumunu
    yakalamak (RETARDO'da yaşanan şey). Her POST_CONFIRM_CHECK_INTERVAL_MINUTES'te
    bir, creator'ın GERÇEK token miktarı (ilk İZLE anındaki baseline'a göre)
    tekrar ölçülür; anlamlı bir düşüş varsa ayrı, acil bir Telegram uyarısı
    gönderilir. Süre dolunca (dev satmadıysa) sessizce izlemeyi bırakır —
    sonsuza kadar RugCheck sorgulamaya devam etmiyoruz."""
    coins_by_mint = coins_by_mint or {}
    now_ts = time.time()
    for mint in list(pending.keys()):
        info = pending[mint]
        if info.get("stage") != "post_confirm":
            continue

        elapsed_min = (now_ts - info["confirmed_ts"]) / 60
        if elapsed_min > POST_CONFIRM_WATCH_MINUTES:
            del pending[mint]  # izleme penceresi doldu, dev satmadıysa sessizce bırak
            continue

        last_check_min = (now_ts - info.get("last_check_ts", info["confirmed_ts"])) / 60
        if last_check_min < POST_CONFIRM_CHECK_INTERVAL_MINUTES:
            continue  # sırası henüz gelmedi

        stored_coin = info.get("coin")
        baseline_amount = info.get("baseline_creator_amount")
        if stored_coin is None or baseline_amount is None or baseline_amount <= 0:
            # İzlenecek anlamlı bir baseline yoksa (dev zaten en başta 0
            # tutuyorduysa satacak bir şeyi de yok demektir) devam etmeye değmez.
            del pending[mint]
            continue

        fresh_coin = dict(coins_by_mint.get(mint, stored_coin))
        time.sleep(0.2)
        apply_dexscreener_data(fresh_coin, mint)

        result = evaluate_candidate(fresh_coin)
        info["last_check_ts"] = now_ts
        if result is None:
            continue  # yaş penceresi hesaplaması bozulduysa bu turu atla, sonraki turda tekrar dene

        new_amount = result.get("creator_holding_amount")
        if new_amount is None:
            continue  # RugCheck bu turda veri vermedi, sonraki check'te tekrar denenir

        drop_pct = (baseline_amount - new_amount) / baseline_amount * 100
        if drop_pct >= DEV_SELL_DROP_THRESHOLD_PCT:
            print(f"[UYARI] {info.get('symbol', '?')} İZLE sonrası dev satışı tespit edildi "
                  f"(~%{drop_pct:.0f} azalma) -> Telegram'a acil uyarı gönderiliyor")
            send_dev_dump_alert(
                info.get("symbol", "?"), info.get("name", "?"), mint,
                baseline_amount, new_amount, drop_pct, elapsed_min,
            )
            del pending[mint]  # uyarı gönderildi, bu coin için izlemeyi bitir
        # drop yoksa: last_check_ts güncellendi, pending'de kalmaya devam eder,
        # süre dolana kadar (POST_CONFIRM_WATCH_MINUTES) tekrar tekrar kontrol edilir.


def run_once(seen, pending, top10_cache, top10_state):
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
        # İZLE sinyali gönderildi diye izlemeyi BIRAKMIYORUZ — dev tam da
        # şimdi satabilir (RETARDO dersi). Bir süre daha (POST_CONFIRM_WATCH_MINUTES)
        # arka planda dev cüzdanını takip etmeye devam ediyoruz.
        mint = c.get("mint")
        if mint and c.get("creator_holding_amount"):
            pending[mint] = {
                "stage": "post_confirm",
                "symbol": c["symbol"], "name": c["name"],
                "coin": c.get("_raw_coin"),
                "baseline_creator_amount": c.get("creator_holding_amount"),
                "confirmed_ts": time.time(),
                "last_check_ts": time.time(),
            }

    # --- AŞAMA B2: İZLE sonrası dev cüzdanı takibi ---
    process_post_confirm_watch(pending, coins_by_mint)

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
        route_first_look_result(result, coin, pending, seen)

    stage_counts = Counter(info.get("stage") for info in pending.values())
    print(f"[{datetime.now().strftime('%H:%M:%S')}] tarandı={len(coins)} | "
          f"yeni_genç={young_added} | olgunlaşan={matured + matured_on_first_sight} | "
          f"onaylanan_izle={len(confirmed)} | beklemede(genç={stage_counts.get('young', 0)}, "
          f"momentum={stage_counts.get('clean_watch', 0)}, "
          f"izle_sonrası_takip={stage_counts.get('post_confirm', 0)})")

    save_pending(pending)

    # --- AŞAMA D: Butona basılmış mı diye Telegram güncellemelerini kontrol et ---
    # (webhook/ayrı sunucu YOK — periyodik olarak getUpdates çağırıyoruz)
    process_telegram_callbacks(top10_cache, top10_state)

    # --- AŞAMA E: Market cap sıralı Top10'u periyodik olarak yayınla ---
    now_ts = time.time()
    last_broadcast_ts = top10_state.get("last_top10_broadcast_ts", 0)
    if now_ts - last_broadcast_ts >= TOP10_BROADCAST_INTERVAL_MINUTES * 60:
        send_top10_by_mcap(coins, top10_cache)
        top10_state["last_top10_broadcast_ts"] = now_ts

    prune_top10_cache(top10_cache)
    save_top10_cache(top10_cache)
    save_top10_state(top10_state)


def main():
    print("DESK Scanner v6 (üç aşamalı: genç takip -> tam değerlendirme -> momentum onayı, "
          "+ market cap sıralı Top10 yayını) başladı. Ctrl+C ile durdur.")
    seen = load_seen()
    pending = load_pending()
    top10_cache = load_top10_cache()
    top10_state = load_top10_state()
    consecutive_errors = 0
    while True:
        try:
            run_once(seen, pending, top10_cache, top10_state)
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
