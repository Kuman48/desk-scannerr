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
import re
import difflib
import requests
from datetime import datetime, timezone
from collections import Counter

# ============================== AYARLAR ==============================
# NOT: Eskiden sabit 60sn'ydi. pump.fun'ın 'en yeni 50' listesi tek, hafif
# bir istek (RugCheck/DexScreener gibi pahalı sorgular burada YAPILMIYOR —
# onlar zaten kendi zamanlayıcılarıyla, sadece SIRASI GELEN adaylar için
# çalışıyor: MIN_AGE_MINUTES, YOUNG_RETRY_INTERVAL_MINUTES, RECHECK_DELAY_
# MINUTES gibi eşikler GERÇEK ZAMANA göre kontrol ediliyor, döngü sıklığına
# göre DEĞİL). Yani döngüyü sıklaştırmak RugCheck/DexScreener çağrı SAYISINI
# neredeyse hiç artırmıyor — sadece "sırası gelen" bir adayı YAKALAMAK için
# beklediğimiz süreyi kısaltıyor (60sn yerine 20sn'de bir bakıyoruz, yani bir
# coin fark edilene kadar ortalama gecikme ~30sn'den ~10sn'ye iniyor).
# Railway'de DESK_POLL_INTERVAL_SEC env variable'ı ile de değiştirilebilir.
POLL_INTERVAL_SEC = int(os.environ.get("DESK_POLL_INTERVAL_SEC", "20"))

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

# --- Öne çıkarma sinyalleri (X/Twitter bağlantısı, yüksek hacim/işlem, yüksek
# holder sayısı) ---
# Bunlar VETO değil, sadece skor BONUSU: varlığı/yüksekliği coin'i öne
# çıkarır ama yokluğu/azlığı tek başına elemez (yeni doğan temiz bir coin'in
# henüz X hesabı ya da yüksek hacmi olmayabilir). "Öne çıkma" = daha yüksek
# skor -> İZLE eşiğini daha kolay geçme ve Top10 sıralamasında öne gelme
# (Top10 zaten mcap'e göre sıralı, skor değil, ama skor İZLE kararını etkiler).
X_LINK_BONUS = 8                      # coin'in X/Twitter bağlantısı varsa
HIGH_VOLUME_M5_USD = 5000             # 5 dk'lık hacim bu eşiği geçerse ekstra bonus
HIGH_VOLUME_BONUS = 10
HIGH_TXN_COUNT_M5 = 20                # 5 dk'lık toplam alım+satım işlemi bu sayıyı geçerse
HIGH_TXN_BONUS = 8
HIGH_HOLDER_COUNT = 30                # holder sayısı bu eşiği geçerse ekstra bonus
HIGH_HOLDER_BONUS = 10

# --- Metadata ---
REQUIRE_METADATA = True       # isim + görsel + açıklama üçü de dolu olmalı

# --- Authority / holder / creator ---
# NOT: Bu eşikler kullanıcının paylaştığı Photon Memescope / GMGN Trenches
# topluluk filtrelerine göre SIKILAŞTIRILDI (eskiden 25'ti). GMGN "geçsin"
# kriteri dev holding için <%8-10 (ideal <%5) diyor; Photon "Newly Created"
# için dev holding %0-5 öneriyor. Eski %25 sınırımız bu standartlara göre
# ÇOK gevşekti.
MAX_CREATOR_HOLD_PCT = 10     # creator bu yüzdeyi TUTUYORSA -> veto (>=10, GMGN'nin "hâlâ tutuyor" sınırı)
CREATOR_IDEAL_HOLD_PCT = 5    # bunun ALTINDAYSA ekstra bonus (Photon'un "ideal" bandı)
CREATOR_IDEAL_BONUS = 5
MAX_TOP5_HOLDER_PCT = 80      # top5 (escrow hariç) bu yüzdeyi TUTUYORSA -> veto (>=80)
TOP10_REDFLAG_PCT = 20        # bu aralık kırmızı bayrak (skor cezası) — Photon "Newly Created" %15-25 bandıyla uyumlu
TOP10_HARD_VETO_PCT = 40      # bunun ÜSTÜ sert veto — GMGN'nin "geçmesin" (>%40-50) sınırıyla uyumlu

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

# --- Momentum kontrolünde "tek anlık fotoğraf" hatası (GROKLER dersi) ---
# Gerçek olay: bir coin $10.655 mcap'te erken sinyal aldı, 5 dk sonraki tek
# kontrolde mcap GEÇİCİ olarak $3.566'ya düşmüştü (-> "market cap çok düşük"
# SERT VETO, kalıcı GEÇ) ama coin daha sonra $122.000'a kadar çıktı — biz
# çoktan vazgeçmiş, bir daha hiç bakmamıştık. Pump.fun coinleri ilk
# dakikalarda çok oynak: geçici bir dip, ölümün değil, sadece o anki kâr
# realizasyonunun işareti olabilir. Bu yüzden momentum kontrolünde SADECE
# İYİLEŞEBİLİR nedenlerle (mcap/holder tekrar düşük çıkması, büyüme yetersiz
# kalması) başarısız olan adayları KALICI OLARAK ELEMİYORUZ — kalıcı/güvenlik
# vetosu (rug, mint/freeze authority, dev satışı vb.) hâlâ anında ve kalıcı
# GEÇ. İYİLEŞEBİLİR olanlara CLEAN_WATCH_MAX_EXTRA_CHECKS kadar ek şans
# tanınır (her biri RECHECK_DELAY_MINUTES sonra); coin zaten genel yaş
# penceresinin (MAX_AGE_MINUTES) dışına çıkarsa otomatik olarak elenir, o
# yüzden bu sonsuza kadar beklemeye dönüşmez.
CLEAN_WATCH_MAX_EXTRA_CHECKS = 2

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

# --- FİYAT TAKİBİ (2-3x PATLAMA UYARISI) ---
# Bu, evaluate_candidate/RugCheck pipeline'ından TAMAMEN AYRI, paralel bir
# mekanizma: "en yeni 50" listesinde gördüğümüz HER coin'in (güvenlik
# kontrolünden geçmiş geçmemiş FARK ETMEKSİZİN) ilk gördüğümüz mcap'ini
# kaydediyoruz, PRICE_WATCH_CHECK_INTERVAL_MINUTES'te bir güncelleyip o
# baseline'a göre kaç katına çıktığını hesaplıyoruz. Belirlenen katları
# (2x, 3x, 5x, 10x) geçtiğinde ayrı bir "FİYAT PATLAMASI" uyarısı
# gönderiyoruz. ÖNEMLİ: Bu SADECE FİYAT HAREKETİNE bakıyor — mint/freeze
# authority, holder dağılımı, rug riski GİBİ HİÇBİR GÜVENLİK KONTROLÜ
# YAPMIYOR. Bu yüzden normal İZLE sinyaliyle KARIŞTIRILMAMALI; mesajda
# bunu her seferinde açıkça belirtiyoruz (DESK asla güvenliymiş gibi
# konuşmaz).
PRICE_WATCH_CHECK_INTERVAL_MINUTES = 2
PRICE_WATCH_MULTIPLIER_TIERS = (2.0, 3.0, 5.0, 10.0)
PRICE_WATCH_MAX_AGE_MINUTES = 30   # bu süreden sonra izlemeyi bırakıyoruz (sonsuza kadar DexScreener sorgulamamak için)
PRICE_WATCH_FILE = os.path.join(DATA_DIR, "desk_price_watch.json")

# --- "BENZER PROFİL" ÖĞRENME KATMANI ---
# Amaç: fiyatı patlayan (price_watch'ta 2x+ yapan) coinlerin İLK GÖRÜLDÜĞÜMÜZ
# ANDAKİ (henüz patlamadan önceki) özelliklerini saklayıp, yeni bir coin
# ilk değerlendirildiğinde bu "kazanan profilleri"ne ne kadar benzediğine
# bakmak. DÜRÜSTLÜK NOTU (önemli): meme coin'lerin BÜYÜK ÇOĞUNLUĞU ilk
# görüldüğü anda BİRBİRİNE ZATEN ÇOK BENZER (düşük curve, küçük mcap) —
# bu yüzden bu benzerlik SADECE bir "dikkat çekici örüntü" notudur, ASLA bir
# tahmin ya da garanti değildir. Örneklem küçükken (WINNER_PROFILE_MIN_
# SAMPLES'tan az) bu kontrol TAMAMEN DEVRE DIŞI kalır — az veriyle yapılan
# "benzerlik" tespiti gürültüden ibarettir.
RECENT_EVALS_FILE = os.path.join(DATA_DIR, "desk_recent_evals.json")   # ilk-bakış özellik anlık görüntüleri (kısa ömürlü)
RECENT_EVALS_MAX_AGE_MINUTES = 90     # bundan eski anlık görüntüler siliniyor (winner-profil eşlemesi için artık işe yaramaz)
WINNER_PROFILES_FILE = os.path.join(DATA_DIR, "desk_winner_profiles.json")
WINNER_PROFILES_MAX_KEEP = 40         # en fazla bu kadar profil tutulur (eskiler piyasa rejimini yansıtmayabilir)
WINNER_PROFILE_MIN_SAMPLES = 5        # bundan AZ profil varsa benzerlik kontrolü YAPILMAZ
SIMILAR_PROFILE_MATCH_THRESHOLD = 0.7  # aşağıdaki özelliklerin en az bu oranı eşleşmeli

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Helius: ücretsiz katmanı olan bir Solana RPC/veri servisi (günlük istek
# limitiyle, kredi kartı GEREKMEZ -- https://dev.helius.xyz üzerinden ücretsiz
# hesap açıp bir API key alınır). RugCheck'in genel raporu bazı coinlerde
# insider cüzdan SAYISINI verse de adreslerin kendisini paylaşmıyor -- bu
# key ayarlanırsa, manuel mint sorgusu (bkz. build_manual_mint_report) kendi
# ÜCRETSİZ "ortak fonlayıcı cüzdan" heuristiğini de çalıştırır (bkz.
# find_shared_funder_clusters). Ayarlanmazsa bu bölüm sessizce atlanır,
# botun geri kalanı normal çalışmaya devam eder.
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY")
HELIUS_TX_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"

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


def load_price_watch():
    """Fiyat takibi (2-3x patlama uyarısı) için ilk-görülen mcap kayıtları."""
    if not os.path.exists(PRICE_WATCH_FILE):
        return {}
    try:
        with open(PRICE_WATCH_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_price_watch(price_watch):
    try:
        with open(PRICE_WATCH_FILE, "w") as f:
            json.dump(price_watch, f)
    except Exception as e:
        print(f"[HATA] Fiyat takibi listesi kaydedilemedi: {e}")


def load_recent_evals():
    if not os.path.exists(RECENT_EVALS_FILE):
        return {}
    try:
        with open(RECENT_EVALS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_recent_evals(recent_evals):
    try:
        with open(RECENT_EVALS_FILE, "w") as f:
            json.dump(recent_evals, f)
    except Exception as e:
        print(f"[HATA] Son değerlendirmeler kaydedilemedi: {e}")


def load_winner_profiles():
    if not os.path.exists(WINNER_PROFILES_FILE):
        return []
    try:
        with open(WINNER_PROFILES_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def save_winner_profiles(profiles):
    try:
        with open(WINNER_PROFILES_FILE, "w") as f:
            json.dump(profiles, f)
    except Exception as e:
        print(f"[HATA] Kazanan profilleri kaydedilemedi: {e}")


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


INSIDER_RISK_KEYWORDS = ("insider", "bundl", "snip", "cluster", "linked wallet", "connected wallet")


def extract_insider_bundler_notes(report, holders):
    """DÜRÜSTLÜK NOTU: Bu, kendi cüzdan-kümeleme (wallet clustering)
    analizimiz DEĞİL — böyle bir analiz Solana zincirindeki işlem
    geçmişini derinlemesine taramayı gerektirir (kim kimi fonladı, hangi
    cüzdanlar aynı blokta aldı), bunu ücretsiz/hafif API'lerle yapamıyoruz.
    Burada SADECE RugCheck'in KENDİ risk motorunun 'insider/bundler/sniper/
    cluster' ile ilgili olarak zaten tespit edip risk listesine eklediği
    (varsa) bulguları ve holder objelerinde varsa 'insider' bayrağını
    yüzeye çıkarıyoruz — level'a bakılmaksızın (sadece 'danger' değil,
    'warn' seviyesindekiler de dahil, çünkü bunlar genelde tam bu
    kategoriye giriyor). RugCheck bu tür bir bulgu koymadıysa liste boş
    döner; bu 'temiz' demek değildir, sadece 'RugCheck'in kendi motoru
    böyle bir şey yakalamadı' demektir."""
    notes = []
    if report:
        for r in (report.get("risks") or []):
            name = (r.get("name") or "")
            if any(kw in name.lower() for kw in INSIDER_RISK_KEYWORDS):
                notes.append(f"{name} ({r.get('level', '?')})")

        # RugCheck'in KENDİ grafik/işlem-tabanlı insider tespiti: bunlar
        # 'risks' listesinden AYRI, üst seviyede duran alanlar --
        # 'graphInsidersDetected' (tespit edilen insider cüzdan SAYISI) ve
        # 'graphInsiderReport' (varsa daha ayrıntılı döküm). Önceden bu iki
        # alanı hiç okumuyorduk; oysa RugCheck'in tek gerçek "cüzdan
        # kümeleme" bulgusu tam olarak burada. Şema RugCheck tarafında
        # değişebildiği için elimizdeki olası anahtarları esnek/güvenli
        # şekilde deniyoruz (yoksa sessizce atlıyoruz).
        insiders_detected = report.get("graphInsidersDetected")
        try:
            insiders_detected = int(insiders_detected) if insiders_detected is not None else None
        except (TypeError, ValueError):
            insiders_detected = None
        if insiders_detected:
            notes.append(f"RugCheck grafik analizi (işlem geçmişi taraması): {insiders_detected} insider cüzdan tespit etti")

        graph_report = report.get("graphInsiderReport")
        if isinstance(graph_report, dict):
            networks = graph_report.get("insiderNetworks") or graph_report.get("networks")
            if isinstance(networks, list) and networks:
                sizes = []
                for net in networks:
                    if isinstance(net, dict):
                        size = net.get("size") or net.get("count") or len(net.get("wallets") or net.get("accounts") or [])
                        if size:
                            sizes.append(size)
                if sizes:
                    notes.append(f"RugCheck {len(networks)} bağlantılı cüzdan ağı (network) buldu (boyutlar: {', '.join(str(s) for s in sizes)})")
                else:
                    notes.append(f"RugCheck {len(networks)} bağlantılı cüzdan ağı (network) buldu")
        elif isinstance(graph_report, list) and graph_report:
            notes.append(f"RugCheck grafik raporu {len(graph_report)} bağlantılı cüzdan grubu listeliyor")

    if holders:
        insider_holders = [h for h in holders if h.get("insider")]
        if insider_holders:
            total_pct = sum(float(h.get("pct", 0)) for h in insider_holders)
            notes.append(f"RugCheck {len(insider_holders)} cüzdanı 'insider' işaretlemiş (toplam ~%{total_pct:.1f})")
    return notes


def extract_insider_wallet_addresses(report, holders):
    """extract_insider_bundler_notes'un yalnızca ÖZET metin ürettiği yerde,
    bu fonksiyon kullanıcının kopyalayabileceği GERÇEK cüzdan adreslerini
    çıkarır -- iki kaynaktan: (1) topHolders içinde 'insider' bayrağı TRUE
    olan holder'ların adresi, (2) graphInsiderReport'taki ağ (network)
    objelerinde varsa adres listesi. Aynı adres iki kaynakta da çıkabilir,
    tekilleştiriyoruz. pct bilgisi varsa (holder kaynaklıysa) yanında
    gösteriyoruz. DÜRÜSTLÜK: bu SADECE RugCheck'in zaten işaretlediği/
    döndürdüğü adresler -- kendi analizimiz değil."""
    result = []  # [(address, pct_or_None)]
    seen_addrs = set()

    if holders:
        for h in holders:
            if not h.get("insider"):
                continue
            addr = h.get("address") or h.get("owner")
            if not addr or addr in seen_addrs:
                continue
            seen_addrs.add(addr)
            try:
                pct = float(h.get("pct", 0))
            except (TypeError, ValueError):
                pct = None
            result.append((addr, pct))

    if report:
        graph_report = report.get("graphInsiderReport")
        networks = None
        if isinstance(graph_report, dict):
            networks = graph_report.get("insiderNetworks") or graph_report.get("networks")
        elif isinstance(graph_report, list):
            networks = graph_report
        if isinstance(networks, list):
            for net in networks:
                if not isinstance(net, dict):
                    continue
                wallets = net.get("wallets") or net.get("accounts") or net.get("addresses") or []
                if not isinstance(wallets, list):
                    continue
                for w in wallets:
                    addr = w if isinstance(w, str) else (w.get("address") or w.get("wallet") if isinstance(w, dict) else None)
                    if not addr or addr in seen_addrs:
                        continue
                    seen_addrs.add(addr)
                    result.append((addr, None))

    return result


def extract_lp_lock_pct(report):
    """RugCheck raporundaki 'markets' listesinden LP (likidite havuzu) kilit
    yüzdesini çıkarır. Kullanıcının istediği 'DexScreener pair sayfasında LP
    lock' kontrolünün karşılığı — ayrı bir servise gerek yok, zaten
    kullandığımız RugCheck raporu bu veriyi (varsa) içeriyor. Pump.fun'da
    henüz mezun olmamış (bonding curve'de) bir coin'in daha ayrı bir LP'si
    olmaz -> bu durumda None dönmesi NORMALDIR, veto sebebi DEĞİLDİR."""
    if not report:
        return None
    markets = report.get("markets")
    if not isinstance(markets, list) or not markets:
        return None
    pcts = []
    for m in markets:
        lp = m.get("lp") if isinstance(m, dict) else None
        if not isinstance(lp, dict):
            continue
        val = lp.get("lpLockedPct")
        if val is not None:
            try:
                pcts.append(float(val))
            except (TypeError, ValueError):
                pass
    if not pcts:
        return None
    return max(pcts)  # en yüksek kilitli havuzu göster (en iyimser/gerçekçi bakış)


# --------------------------- ÜCRETSİZ HEURİSTİK: ORTAK FONLAYICI CÜZDAN (Helius) ---------------------------
# DÜRÜSTLÜK NOTU (ÇOK ÖNEMLİ): Bu GERÇEK bir cüzdan-kümeleme (wallet
# clustering) motoru DEĞİL -- sadece basit bir HEURİSTİK: "top holder'lardan
# birden fazlası, alım yapmadan kısa süre önce AYNI cüzdandan SOL aldıysa,
# muhtemelen aynı kişi/grup tarafından önceden fonlanmışlardır (insider/
# bundler paterni)". Bu YANLIŞ POZİTİF üretebilir -- ör. bir borsa (CEX) hot
# wallet'ından çekim yapan iki farklı, birbiriyle alakasız kullanıcı da aynı
# "ortak fonlayıcı"ya sahip görünebilir. Bu yüzden bir SİNYAL/İPUCU olarak
# gösteriliyor, KESİN KANIT değil. Sadece HELIUS_API_KEY ayarlıysa çalışır
# (ücretsiz katman, https://dev.helius.xyz).

def fetch_helius_wallet_transactions(address, limit=25):
    """Bir cüzdanın SON işlemlerini Helius'un 'enhanced transactions'
    API'sinden (parse edilmiş, SOL transferleri dahil) çeker. API key
    ayarlı değilse veya istek başarısız olursa None döner (çağıran kod bu
    cüzdanı atlar, tüm sorguyu ÇÖKERTMEZ)."""
    if not HELIUS_API_KEY or not address:
        return None
    try:
        url = HELIUS_TX_URL.format(address=address)
        resp = requests.get(url, params={"api-key": HELIUS_API_KEY, "limit": limit}, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, list) else None
    except Exception as e:
        print(f"[HATA] Helius cüzdan geçmişi çekilemedi ({address}): {type(e).__name__}: {e}")
        return None


def find_incoming_sol_funders(address, limit=25):
    """Bir cüzdana SON zamanlarda SOL göndermiş (gelen native transfer)
    KAYNAK adresleri döner (tekilleştirilmiş küçük bir küme). Cüzdanın
    'nereden fonlandığını' kabaca anlamak için kullanılıyor -- tam/kesin
    değil, sadece son birkaç işleme bakıyor (ücretsiz katmanın istek
    sayısını makul tutmak için limit düşük tutuluyor)."""
    txs = fetch_helius_wallet_transactions(address, limit=limit)
    if not txs:
        return set()
    funders = set()
    for tx in txs:
        for transfer in (tx.get("nativeTransfers") or []):
            if transfer.get("toUserAccount") == address:
                frm = transfer.get("fromUserAccount")
                if frm and frm != address:
                    funders.add(frm)
    return funders


def find_shared_funder_clusters(holder_addresses, max_wallets=15):
    """Verilen holder adreslerinin (en fazla max_wallets tanesi -- ücretsiz
    katmanın istek sayısını sınırlı tutmak için) her biri için Helius'tan
    'son zamanlarda kimden SOL aldı' bilgisini çeker, sonra AYNI fonlayıcı
    cüzdandan gelen 2+ holder'ı bir 'küme' (cluster) olarak gruplar. Bu
    kümeler, tek bir kişi/grubun birden fazla cüzdanla aynı coin'i
    aldığının (insider/bundler paterni) bir İPUCU'dur -- kanıt değil.
    HELIUS_API_KEY yoksa boş liste döner (özellik sessizce devre dışı)."""
    if not HELIUS_API_KEY or not holder_addresses:
        return []

    funder_to_holders = {}
    for addr in holder_addresses[:max_wallets]:
        funders = find_incoming_sol_funders(addr)
        for funder in funders:
            funder_to_holders.setdefault(funder, set()).add(addr)

    clusters = [
        (funder, sorted(holders_set))
        for funder, holders_set in funder_to_holders.items()
        if len(holders_set) >= 2
    ]
    clusters.sort(key=lambda c: len(c[1]), reverse=True)
    return clusters


# --------------------------- MANUEL MINT SORGUSU (Telegram'a coin atma) ---------------------------
# Kullanıcı bota düz metin olarak bir mint adresi (veya pump.fun/DexScreener
# linki) attığında, kendi tarama pipeline'ımızın dışında, O ANDA RugCheck +
# DexScreener sorgulayıp elimizdeki bulguları (insider/bundler notları dahil)
# özetleyen bağımsız bir "anlık rapor" özelliği. DÜRÜSTLÜK: bu, coin bizim
# 2 dakikalık yeni-coin taramamızdan geçmediği için creator geçmişi
# (kaç coin çıkarmış) gibi bazı alanları göremeyebilir; sadece RugCheck +
# DexScreener'ın o an dönebildiği kadarını gösteriyoruz.
MINT_ADDRESS_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def extract_mint_from_text(text):
    """Düz metinden (ham mint adresi VEYA pump.fun/DexScreener/Birdeye/
    Solscan linki) bir Solana mint adresi çıkarmaya çalışır. Bulamazsa None
    döner."""
    if not text:
        return None
    text = text.strip()
    match = MINT_ADDRESS_RE.search(text)
    if not match:
        return None
    return match.group(0)


def build_manual_mint_report(mint):
    """Kullanıcının bota attığı bir mint adresi için RugCheck + DexScreener'ı
    o an sorgulayıp Telegram'a gönderilecek HTML metnini üretir. Kendi
    tarama/skorlama pipeline'ımızdan (evaluate_candidate) BAĞIMSIZDIR --
    bu yüzden sert veto/skor üretmiyor, sadece elimizdeki ham bulguları
    dürüstçe özetliyor."""
    report = fetch_rugcheck_report(mint)
    dex_data = fetch_dexscreener_data(mint)

    if report is None and dex_data is None:
        return (
            f"⚠️ <b>Bulunamadı</b>\n\n"
            f"🧾 <code>{esc(mint)}</code> için ne RugCheck ne de DexScreener'dan "
            f"veri alabildim. Adres yanlış olabilir, ya da bu coin/token için "
            f"henüz hiçbir kayıt indekslenmemiş olabilir."
        )

    symbol = None
    name = None
    if report:
        token_meta = report.get("tokenMeta") or {}
        symbol = token_meta.get("symbol")
        name = token_meta.get("name")

    holders = None
    top5pct = top10pct = None
    creator = None
    creator_pct = None
    if report:
        raw_holders = report.get("topHolders")
        if isinstance(raw_holders, list):
            holders = sorted(raw_holders, key=lambda h: float(h.get("pct", 0)), reverse=True)
            top5pct = sum_pct(holders, 5)
            top10pct = sum_pct(holders, 10)
        # RugCheck bazı raporlarda üst seviyede 'creator' alanı veriyor;
        # yoksa creator-özel alanları (creator payı) gösteremeyiz -- bunu
        # açıkça belirtiyoruz, "creator temiz" gibi yanlış bir izlenim
        # vermemek için.
        creator = report.get("creator") or (report.get("token") or {}).get("creator")
        if creator and holders:
            creator_pct = get_creator_holding_pct(holders, creator)

    mint_auth, freeze_auth = extract_authorities(report) if report else (None, None)
    rc_score = extract_rugcheck_score(report)
    danger_risks = extract_danger_risks(report) if report else []
    insider_notes = extract_insider_bundler_notes(report, holders)
    lp_lock_pct = extract_lp_lock_pct(report) if report else None
    rugged_flag = bool(report.get("rugged")) if report else None

    mcap = dex_data.get("mcap") if dex_data else None
    volume_m5 = dex_data.get("volume_m5") if dex_data else None
    buys_m5 = dex_data.get("buys_m5") if dex_data else None
    sells_m5 = dex_data.get("sells_m5") if dex_data else None

    lines = [
        f"🔎 <b>Manuel sorgu — ${esc(symbol) if symbol else '?'}</b>"
        + (f" ({esc(name)})" if name else ""),
        "",
        f"💰 Market cap: {esc(f'${mcap:,.0f}' if mcap is not None else 'doğrulanamadı')}",
        f"📊 5dk hacim: {esc(f'${volume_m5:,.0f}' if volume_m5 is not None else 'veri yok')}"
        + (f"   |  🔁 5dk işlem: {buys_m5 or 0} alım / {sells_m5 or 0} satım" if buys_m5 is not None or sells_m5 is not None else ""),
        "",
        f"🪙 Mint authority: {esc('aktif (RİSK)' if mint_auth else ('kapalı' if report else 'doğrulanamadı'))}",
        f"❄️ Freeze authority: {esc('aktif (RİSK)' if freeze_auth else ('kapalı' if report else 'doğrulanamadı'))}",
        f"🧪 RugCheck skoru: {esc(f'{rc_score:.0f}/100' if rc_score is not None else 'doğrulanamadı')}",
        f"🚩 'Rugged' işaretli mi: {esc('EVET' if rugged_flag else ('hayır' if report else 'doğrulanamadı'))}",
    ]
    if top5pct is not None:
        lines.append(f"👥 Top5 holder: %{top5pct:.1f}   Top10: %{top10pct:.1f}")
    if creator:
        lines.append(
            f"🕵️ Creator payı: {esc(f'%{creator_pct:.1f}' if creator_pct is not None else 'doğrulanamadı')}"
        )
    else:
        lines.append("🕵️ Creator payı: bu modda tespit edilemedi (RugCheck raporunda creator alanı yok)")
    lines.append(
        f"🔒 LP kilidi: {esc(f'%{lp_lock_pct:.0f} kilitli' if lp_lock_pct is not None else ('henüz uygulanamaz/doğrulanamadı' if report else 'doğrulanamadı'))}"
    )
    if danger_risks:
        lines.append(f"🚨 RugCheck 'danger' riskleri: {esc(', '.join(danger_risks))}")

    # RugCheck'in kendi işlem-grafiği taramasından çıkan HAM sayı -- notlar
    # boş olsa bile bunu ayrı gösteriyoruz, çünkü "0 tespit edildi" ile
    # "RugCheck bu alanı hiç doldurmadı/veri yok" birbirinden FARKLI şeyler.
    graph_insiders = report.get("graphInsidersDetected") if report else None
    try:
        graph_insiders = int(graph_insiders) if graph_insiders is not None else None
    except (TypeError, ValueError):
        graph_insiders = None
    if graph_insiders is not None:
        lines.append(f"🕸️ RugCheck grafik taraması — tespit edilen insider cüzdan sayısı: {graph_insiders}")

    lines.append(
        "🕵️‍♂️ Insider/bundler notu: " + (esc("; ".join(insider_notes)) if insider_notes else "RugCheck böyle bir şey işaretlememiş (bu 'temiz' demek değil, sadece RugCheck'in kendi motoru bir şey yakalamadı demek)")
    )

    # Kopyalanabilir cüzdan adresleri -- her biri kendi <code> satırında,
    # Telegram'da tek dokunuşla kopyalanabiliyor. NOT: RugCheck'in genel/
    # ücretsiz rapor endpoint'i bazen sadece bir SAYI veriyor (graphInsiders
    # Detected), adreslerin kendisini değil -- bu durumda listeyi boş
    # döndürüyoruz ve bunu AÇIKÇA söylüyoruz (sayı var ama adres yok demek,
    # "adres bulunamadı" demek değil, "RugCheck bu rapor seviyesinde
    # paylaşmıyor" demek).
    wallet_addrs = extract_insider_wallet_addresses(report, holders)
    if wallet_addrs:
        lines.append("")
        lines.append(f"📋 <b>Kopyalanabilir insider cüzdan adresleri</b> ({len(wallet_addrs)}):")
        for addr, pct in wallet_addrs[:25]:
            pct_txt = f" (%{pct:.1f})" if pct is not None else ""
            lines.append(f"<code>{esc(addr)}</code>{pct_txt}")
        if len(wallet_addrs) > 25:
            lines.append(f"... ve {len(wallet_addrs) - 25} tane daha (mesaj sınırı yüzünden kesildi)")
    elif graph_insiders:
        lines.append("")
        lines.append(
            f"📋 RugCheck grafik taraması {graph_insiders} insider cüzdan SAYISI verdi ama bu genel/ücretsiz "
            f"rapor seviyesinde adreslerin kendisini paylaşmıyor -- adres listesi için RugCheck'in "
            f"sitesindeki bağlantıya bak (aşağıda)."
        )

    # ÜCRETSİZ HEURİSTİK (Helius): top holder'lardan 2+'sı aynı cüzdandan
    # SOL almışsa (muhtemelen aynı kişi tarafından önceden fonlandılarsa)
    # burada gösteriyoruz. Sadece HELIUS_API_KEY ayarlıysa çalışır.
    if not HELIUS_API_KEY:
        lines.append("")
        lines.append(
            "🔬 Ücretsiz ek heuristik (ortak fonlayıcı cüzdan taraması) kapalı -- "
            "açmak için Railway'de HELIUS_API_KEY değişkenini ayarla (ücretsiz hesap: dev.helius.xyz)."
        )
    elif holders:
        holder_addrs = [h.get("address") or h.get("owner") for h in holders if h.get("address") or h.get("owner")]
        clusters = find_shared_funder_clusters(holder_addrs, max_wallets=10)
        lines.append("")
        if clusters:
            lines.append(f"🔬 <b>Ücretsiz heuristik — ortak fonlayıcı cüzdan tespiti</b> (ipucu, kanıt değil):")
            for funder, funded in clusters[:5]:
                lines.append(f"Fonlayıcı: <code>{esc(funder)}</code> → {len(funded)} top holder'ı fonlamış:")
                for addr in funded[:10]:
                    lines.append(f"  <code>{esc(addr)}</code>")
            lines.append(
                "⚠️ Bu bir borsa (CEX) çekim cüzdanı da olabilir (yanlış pozitif) -- "
                "kesin insider kanıtı DEĞİL, sadece araştırmaya değer bir ipucu."
            )
        else:
            lines.append(
                "🔬 Ücretsiz heuristik (ortak fonlayıcı cüzdan taraması): top holder'lar arasında "
                "ortak bir fonlayıcı bulunamadı (ilk 10 holder, sınırlı işlem geçmişi taramasıyla -- "
                "bu 'temiz' demek değil, sadece bu dar taramada bir şey çıkmadı demek)."
            )

    lines.append("")
    lines.append(f"🧾 <code>{esc(mint)}</code>")
    lines.append(
        f'🔗 <a href="https://pump.fun/coin/{mint}">pump.fun</a> | '
        f'<a href="https://dexscreener.com/solana/{mint}">DexScreener</a> | '
        f'<a href="https://rugcheck.xyz/tokens/{mint}">RugCheck</a>'
    )
    lines.append("")
    lines.append(
        "⚠️ Bu, gerçek bir cüzdan-kümeleme (wallet clustering) analizi DEĞİL — "
        "sadece RugCheck + DexScreener'ın o an dönebildiği veriler. Yatırım "
        "tavsiyesi değildir."
    )
    return "\n".join(lines)


def handle_manual_mint_lookup(message):
    """Kullanıcının bota düz metin olarak attığı bir mesajdan mint adresi
    çıkarmaya çalışır; bulursa raporu üretip aynı sohbete gönderir. Bizim
    kendi chat'imiz DIŞINDAN gelen mesajları YOK SAYAR (güvenlik)."""
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if not TELEGRAM_CHAT_ID or chat_id != str(TELEGRAM_CHAT_ID):
        return
    text = message.get("text") or ""
    if not text or text.startswith("/"):
        return  # komutları (varsa ileride) veya boş mesajları görmezden gel
    mint = extract_mint_from_text(text)
    if not mint:
        return  # mint adresine benzemiyor -> sessizce yok say, spam yapma
    send_telegram_message(f"🔎 <code>{esc(mint)}</code> sorgulanıyor (RugCheck + DexScreener)...")
    report_text = build_manual_mint_report(mint)
    send_telegram_message(report_text)


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

    # --- X/Twitter bağlantısı (öne çıkarma sinyali, veto yok) ---
    twitter_link = (coin.get("twitter") or "").strip()
    has_twitter = bool(twitter_link)
    if has_twitter:
        score += X_LINK_BONUS
        score_notes.append(f"+{X_LINK_BONUS} X/Twitter bağlantısı var, öne çıkıyor")

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
        lp_lock_display = "doğrulanamadı"
        top10pct = None
        rc_risk_score = None
        insider_notes = []
    else:
        mint_auth, freeze_auth = extract_authorities(report)
        holders = get_non_escrow_holders(report, coin)
        top5pct = sum_pct(holders, 5)
        top10pct = sum_pct(holders, 10)
        creator_pct = get_creator_holding_pct(holders, creator)
        creator_amount = get_creator_holding_amount(holders, creator)
        rc_risk_score = extract_rugcheck_score(report)
        danger_risks = extract_danger_risks(report)
        insider_notes = extract_insider_bundler_notes(report, holders)
        rugged_flag = bool(report.get("rugged"))
        distinct_holder_count = len(holders) if holders is not None else None

        # --- LP (likidite) kilit yüzdesi (kullanıcının istediği "LP lock" kontrolü) ---
        # Sadece mezun olmuş (Raydium/PumpSwap'e geçmiş) coinlerde anlamlıdır;
        # hâlâ bonding curve'de olan bir coin'in ayrı bir LP'si yoktur, bu
        # yüzden None -> veto DEĞİL, sadece "henüz uygulanamaz" demektir.
        lp_lock_pct = extract_lp_lock_pct(report)
        if lp_lock_pct is not None:
            lp_lock_display = f"%{lp_lock_pct:.0f} kilitli"
        elif complete:
            lp_lock_display = "doğrulanamadı (mezun oldu ama LP kilit verisi yok)"
        else:
            lp_lock_display = "henüz uygulanamaz (bonding curve'de, ayrı LP yok)"

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
            if distinct_holder_count >= HIGH_HOLDER_COUNT:
                score += HIGH_HOLDER_BONUS
                score_notes.append(f"+{HIGH_HOLDER_BONUS} holder sayısı çok yüksek (>= {HIGH_HOLDER_COUNT}), öne çıkıyor")

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
            if creator_pct < CREATOR_IDEAL_HOLD_PCT:
                score += CREATOR_IDEAL_BONUS
                score_notes.append(f"+{CREATOR_IDEAL_BONUS} creator payı ideal bantta (<%{CREATOR_IDEAL_HOLD_PCT})")

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

        # --- Insider/bundler/sniper (SADECE RugCheck'in kendi motoru zaten
        # tespit ettiyse) — 'danger' seviyesindekiler zaten yukarıda sert
        # veto aldı; burada kalanlar (warn vb.) veto DEĞİL, sadece skor
        # cezası + görünür not. Bu bizim kendi cüzdan analizimiz değil. ---
        if insider_notes:
            score -= 8
            score_notes.append(f"-8 RugCheck insider/bundler/sniper bulgusu: {', '.join(insider_notes)}")

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

        # --- Tek yönlü pump uyarısı (Photon/GMGN notu: "sells de olsun, tek
        # yönlü pump şüpheli") — anlamlı alım hacmi varken HİÇ satış yoksa
        # bu organik olmayabilir (örn. wash trading ya da satışı engelleyen
        # bir mekanizma). Veto değil, sadece dikkat notu. ---
        if buys_m5 >= 10 and sells_m5 == 0:
            score -= 5
            score_notes.append(f"-5 tek yönlü pump (5dk: {buys_m5} alım, 0 satım — organik olmayabilir)")

        # --- Öne çıkarma: yüksek işlem sayısı / yüksek hacim (5dk) ---
        total_txns_m5 = buys_m5 + sells_m5
        if total_txns_m5 >= HIGH_TXN_COUNT_M5:
            score += HIGH_TXN_BONUS
            score_notes.append(f"+{HIGH_TXN_BONUS} işlem sayısı çok yüksek (5dk: {total_txns_m5} işlem), öne çıkıyor")
        if volume_m5 is not None and volume_m5 >= HIGH_VOLUME_M5_USD:
            score += HIGH_VOLUME_BONUS
            score_notes.append(f"+{HIGH_VOLUME_BONUS} hacim çok yüksek (5dk: ${volume_m5:,.0f}), öne çıkıyor")
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
        "has_twitter": has_twitter, "twitter_link": twitter_link or None,
        "creator_prior_coins": prior_count,
        "mint_authority": mint_auth_display, "freeze_authority": freeze_auth_display,
        "creator_holding_pct": creator_pct_display,
        "creator_holding_pct_value": creator_pct,      # sayısal (karşılaştırma için)
        "creator_holding_amount": creator_amount,      # GERÇEK token miktarı (dev satışı tespiti için)
        "creator_holds": creator_holds_display,        # "şu an dev'in elinde coin var mı" (evet/hayır/doğrulanamadı)
        "top5_holder_pct": top5_display, "top10_holder_pct": top10_display,
        "top10_holder_pct_value": top10pct,   # sayısal (benzerlik karşılaştırması için)
        "rugcheck_risk_score": rc_score_display,
        "rugcheck_risk_score_value": rc_risk_score,   # sayısal (benzerlik karşılaştırması için)
        "lp_lock_info": lp_lock_display,
        "insider_notes": insider_notes,   # RugCheck'in kendi motorunun yakaladığı insider/bundler/sniper bulguları (varsa)
        "score": round(score, 1), "score_notes": score_notes,
        "hard_vetoes": hard_vetoes, "decision": decision,
        "has_permanent_veto": has_permanent_veto,
        "dex_volume_info": volume_display,          # "X alım / Y satım (5dk), hacim $Z"
        "dex_buys_m5": buys_m5, "dex_sells_m5": sells_m5, "dex_volume_m5": volume_m5,
        "pumpfun_url": f"https://pump.fun/coin/{mint}" if mint else None,
        "dexscreener_url": f"https://dexscreener.com/solana/{mint}" if mint else None,
        "image_uri": coin.get("image_uri"),
    }


# --------------------------- "BENZER PROFİL" ÖĞRENME KATMANI ---------------------------
# Bkz. AYARLAR'daki WINNER_PROFILES bloğu için dürüstlük notu. Özetle: bu
# katman bir TAHMİN MOTORU DEĞİL, sadece "geçmişte patlayan coinlerin ilk
# görüldüğü andaki birkaç sayısal/ikili özelliğine ne kadar benziyor" diye
# bakan kaba bir kural tabanlı karşılaştırma. Örneklem küçükken kapalı kalır.

FEATURE_TOLERANCES = {
    # (özellik adı, alanı result/profile dict'te aynı isimle): tolerans
    "curve_pct": 15,                      # yüzde puan
    "top10_holder_pct_value": 15,         # yüzde puan
    "creator_holding_pct_value": 5,       # yüzde puan
    "distinct_holders_count": 8,          # adet
    "rugcheck_risk_score_value": 20,      # 0-100 skalada puan
}


def build_feature_snapshot(result):
    """evaluate_candidate() sonucundan, benzerlik karşılaştırmasında
    kullanılacak sayısal/ikili özellikleri çıkarır."""
    snap = {k: result.get(k) for k in FEATURE_TOLERANCES}
    snap["has_twitter"] = bool(result.get("has_twitter"))
    return snap


def record_recent_eval(recent_evals, mint, result):
    """Her İLK tam değerlendirmede (route_first_look_result) çağrılır —
    coin daha patlamadan ÖNCEKİ özelliklerini saklar. Sadece price_watch
    sonradan 2x+ yaparsa bu anlık görüntü bir 'winner profile'a dönüşür."""
    recent_evals[mint] = {
        "symbol": result.get("symbol"), "ts": time.time(),
        "features": build_feature_snapshot(result),
    }


def prune_recent_evals(recent_evals):
    now_ts = time.time()
    for mint in list(recent_evals.keys()):
        age_min = (now_ts - recent_evals[mint].get("ts", 0)) / 60
        if age_min > RECENT_EVALS_MAX_AGE_MINUTES:
            del recent_evals[mint]


def promote_to_winner_profile(winner_profiles, recent_evals, mint, symbol, tier):
    """price_watch bir mint'in İLK KEZ 2x (ya da üstü) yaptığını
    doğruladığında çağrılır. O mint için daha önce saklanmış bir ilk-bakış
    anlık görüntüsü VARSA (yani coin bir kez tam değerlendirmeden geçmişse),
    bunu 'kazanan profili' olarak kalıcı listeye ekler. Yoksa (coin hiç tam
    değerlendirilmeden patladıysa) SESSİZCE ATLANIR — elimizde öğrenecek
    özellik verisi yok demektir."""
    snap = recent_evals.get(mint)
    if not snap:
        return
    winner_profiles.append({
        "symbol": symbol, "mint": mint, "tier": tier,
        "recorded_ts": time.time(),
        "features": snap["features"],
    })
    del winner_profiles[:-WINNER_PROFILES_MAX_KEEP]  # sadece son N'i tut


def feature_similarity(features_a, features_b):
    """İki özellik anlık görüntüsünü karşılaştırır. Sadece HER İKİSİNDE DE
    mevcut olan (None olmayan) alanlar sayılır. En az 3 karşılaştırılabilir
    alan yoksa (None, None) döner — yargı vermek için yetersiz veri demektir."""
    matched = 0
    comparable = 0
    for feat, tolerance in FEATURE_TOLERANCES.items():
        a, b = features_a.get(feat), features_b.get(feat)
        if a is None or b is None:
            continue
        comparable += 1
        if abs(a - b) <= tolerance:
            matched += 1
    if features_a.get("has_twitter") is not None and features_b.get("has_twitter") is not None:
        comparable += 1
        if bool(features_a["has_twitter"]) == bool(features_b["has_twitter"]):
            matched += 1
    if comparable < 3:
        return None
    return matched / comparable


def find_similar_winner_profile(result, winner_profiles):
    """Örneklem küçükse (WINNER_PROFILE_MIN_SAMPLES'tan az) None döner —
    bu kontrol DEVRE DIŞI demektir. Yeterli örneklem varsa, en iyi eşleşen
    profili (varsa) ve eşleşme oranını döner."""
    if len(winner_profiles) < WINNER_PROFILE_MIN_SAMPLES:
        return None
    candidate_features = build_feature_snapshot(result)
    best = None
    for profile in winner_profiles:
        ratio = feature_similarity(candidate_features, profile["features"])
        if ratio is None:
            continue
        if ratio >= SIMILAR_PROFILE_MATCH_THRESHOLD and (best is None or ratio > best[1]):
            best = (profile, ratio)
    return best


def send_similar_profile_alert(result, profile, ratio, sample_size):
    """Ayrı, açıkça uyarılı bir bildirim — ne 'erken sinyal' ne 'İZLE'
    formatıyla karıştırılmasın diye kendi etiketini kullanıyor."""
    text = (
        f"🧭 <b>{esc(result['symbol'])}</b> (<i>{esc(result['name'])}</i>) — <b>BENZER PROFİL</b>\n"
        f"Daha önce {profile['tier']:.0f}x yapan <b>${esc(profile['symbol'])}</b> ile ilk-bakış "
        f"özellikleri %{ratio * 100:.0f} oranında örtüşüyor (şu ana kadar biriken {sample_size} "
        f"örnek üzerinden).\n\n"
        f"⚠️ Bu bir TAHMİN DEĞİL — sadece birkaç yüzeysel özelliğin (curve, holder dağılımı, "
        f"dev payı, Twitter varlığı) geçmişte patlayan bir coinle örtüşmesi. Örneklem hâlâ "
        f"küçük, meme coinlerin büyük çoğunluğu zaten ilk bakışta birbirine benzer görünür ve "
        f"hiçbir zaman büyümez. Yatırım tavsiyesi değildir.\n\n"
        f"💯 {result['score']}/100 · ⏱ {result['age_min']}dk · 💰 ${result['mcap']:.0f}\n"
        f'🔗 <a href="{result["pumpfun_url"]}">pump.fun</a>'
        + (f' | <a href="{result["dexscreener_url"]}">DexScreener</a>' if result.get("dexscreener_url") else "")
        + f"\n🧾 <code>{esc(result['mint'])}</code>"
    )
    send_telegram_message(text)


# --------------------------- BİLDİRİM ---------------------------

def build_highlight_tags(has_twitter=False, volume_m5=None, buys_m5=None, sells_m5=None, holder_count=None):
    """Tüm mesaj tiplerinde (Top10, erken sinyal, onaylanan İZLE) AYNI kısa
    etiket setini üretir: 𝕏 / 💰hacim / 🔁işlem / 👥holder. Bunlar veto değil,
    sadece dikkat çekmesi gereken güçlü noktalar — tutarlılık için tek yerden
    üretiliyor."""
    tags = []
    if has_twitter:
        tags.append("𝕏")
    if volume_m5 is not None and volume_m5 >= HIGH_VOLUME_M5_USD:
        tags.append("💰hacim")
    if (buys_m5 or 0) + (sells_m5 or 0) >= HIGH_TXN_COUNT_M5:
        tags.append("🔁işlem")
    if holder_count is not None and holder_count >= HIGH_HOLDER_COUNT:
        tags.append("👥holder")
    return tags


def highlight_tags_line(c):
    tags = build_highlight_tags(
        has_twitter=bool(c.get("has_twitter")),
        volume_m5=c.get("dex_volume_m5"),
        buys_m5=c.get("dex_buys_m5"),
        sells_m5=c.get("dex_sells_m5"),
        holder_count=c.get("distinct_holders_count"),
    )
    return f"  [{' '.join(tags)}]" if tags else ""


def format_message(c):
    lines = [
        f"### {c['symbol']} ({c['name']})  |  Skor: {c['score']}/100  |  Karar: {c['decision']}",
        f"- Kontrat: {c['mint']}",
        f"- Yaş: {c['age_min']} dk | Market cap: ${c['mcap']:.0f} | Curve ~%{c['curve_pct']}",
    ]
    if c.get("momentum_note"):
        lines.append(f"- Momentum (2. bakış): {c['momentum_note']}")
    lines += [
        f"- X/Twitter: {c.get('twitter_link') or 'yok'}",
        f"- Alım/satım (5dk, DexScreener): {c.get('dex_volume_info', 'doğrulanamadı')}",
        f"- Farklı holder sayısı (proxy, gerçek unique-alıcı değil): {c['distinct_holders']}",
        f"- Dev/creator şu an token tutuyor mu: {c.get('creator_holds', 'doğrulanamadı')}",
        f"- Creator önceki coin sayısı: {c['creator_prior_coins']}",
        f"- Mint authority: {c['mint_authority']} | Freeze authority: {c['freeze_authority']}",
        f"- Creator holding: {c['creator_holding_pct']} | Top5: {c['top5_holder_pct']} | Top10: {c['top10_holder_pct']}",
        f"- RugCheck risk skoru: {c['rugcheck_risk_score']}",
        f"- LP kilidi: {c.get('lp_lock_info', 'doğrulanamadı')}",
        f"- Insider/bundler (RugCheck): {', '.join(c['insider_notes']) if c.get('insider_notes') else 'RugCheck bir şey işaretlemedi'}",
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
        f"{emoji} <b>{esc(c['symbol'])}</b>  (<i>{esc(c['name'])}</i>){highlight_tags_line(c)}\n"
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
        f"• LP kilidi: {esc(c.get('lp_lock_info', 'doğrulanamadı'))}",
        f"• Insider/bundler (RugCheck): {esc(', '.join(c['insider_notes']) if c.get('insider_notes') else 'RugCheck bir şey işaretlemedi')}",
        "",
        "📊 <b>Alım/Satım (5dk, DexScreener)</b>",
        f"• {esc(c.get('dex_volume_info', 'doğrulanamadı'))}",
        "",
        "👥 <b>Holder Dağılımı</b>",
        f"• Farklı holder: {esc(c['distinct_holders'])} <i>(proxy, gerçek unique-alıcı değil)</i>"
        + (" 🔥 çok yüksek" if (c.get("distinct_holders_count") or 0) >= HIGH_HOLDER_COUNT else ""),
        "• X/Twitter: " + (
            '<a href="{}">bağlantı var</a>'.format(esc(c["twitter_link"])) if c.get("twitter_link") else "yok"
        ),
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
        # Telegram'ın sendMessage limiti 4096 karakter -- aşarsa 400 döner ve
        # mesaj hiç gitmez. Güvenlik payı bırakarak kesiyoruz.
        safe_text = text_html if len(text_html) <= 4000 else text_html[:3980] + "\n\n[...mesaj kısaltıldı]"
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": safe_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        # ÖNEMLİ: requests.post() 400/403 gibi hatalarda kendiliğinden
        # exception FIRLATMAZ -- eskiden burada status kontrolü yoktu, yani
        # örneğin mesaj 4096 karakteri aşarsa ya da HTML etiketi bozuksa
        # Telegram 400 dönüyordu ama biz bunu hiç loglamıyorduk (mesaj
        # sessizce kayboluyordu). Artık her başarısız durumu logluyoruz.
        if resp.status_code != 200:
            print(f"[HATA] Telegram mesajı reddedildi (HTTP {resp.status_code}): {resp.text[:300]}")
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
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text_html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": json.dumps({"inline_keyboard": keyboard_rows}),
        }, timeout=10)
        if resp.status_code != 200:
            print(f"[HATA] Telegram klavyeli mesajı reddedildi (HTTP {resp.status_code}): {resp.text[:300]}")
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
            # "message" eklendi: kullanıcı bota düz metin olarak bir mint
            # adresi/link atabilsin diye (bkz. handle_manual_mint_lookup).
            "allowed_updates": json.dumps(["callback_query", "message"]),
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
    mint_ok = "revoked" in str(c.get("mint_authority", "")).lower() or "null" in str(c.get("mint_authority", "")).lower()
    freeze_ok = "revoked" in str(c.get("freeze_authority", "")).lower() or "null" in str(c.get("freeze_authority", "")).lower()
    guvenlik = f"mint {'✅' if mint_ok else '⚠️'} / freeze {'✅' if freeze_ok else '⚠️'} / risk {esc(c['rugcheck_risk_score'])}"

    dex_line = ""
    dex_info = c.get("dex_volume_info")
    if dex_info and "doğrulanamadı" not in dex_info:
        dex_line = f"\n📊 {esc(dex_info)}"

    lines = [
        f"🔍 <b>{esc(c['symbol'])}</b> (<i>{esc(c['name'])}</i>) — <b>ERKEN SİNYAL</b>{highlight_tags_line(c)}",
        f"💯 {c['score']}/100  ·  ⏱ {c['age_min']}dk  ·  💰 ${c['mcap']:.0f}  ·  curve %{c['curve_pct']}",
        f"🔐 {guvenlik}",
        f"👥 Holder: {esc(c['distinct_holders'])}  ·  Top10: {esc(c['top10_holder_pct'])}  ·  Dev elinde token: {esc(c.get('creator_holds', '?'))}"
        + dex_line,
        "",
        f"⚠️ Kesin İZLE DEĞİL — {RECHECK_DELAY_MINUTES} dk sonra büyüme (mcap/holder) teyit edilecek, sonuç ayrı mesajla gelecek.",
        "",
        f'🔗 <a href="{c["pumpfun_url"]}">pump.fun</a>'
        + (f' | <a href="{c["dexscreener_url"]}">DexScreener</a>' if c.get("dexscreener_url") else "")
        + (f' | <a href="{esc(c["twitter_link"])}">X</a>' if c.get("twitter_link") else ""),
        f"🧾 <code>{esc(c['mint'])}</code>",
    ]
    caption = "\n".join(lines)

    photo_sent = send_telegram_photo(c.get("image_uri"), caption)
    if not photo_sent:
        send_telegram_message(caption)


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

        # --- Öne çıkarma sinyalleri: X bağlantısı (ücretsiz, coin verisinden),
        # DexScreener hacim/işlem (ücretsiz, sadece bu 10 coin için — 50'nin
        # hepsi için sorgulamıyoruz), holder sayısı (RugCheck, yine sadece
        # bu 10 coin için). Sıralamayı DEĞİŞTİRMEZ, sadece listede etiket
        # olarak gösterir. ---
        time.sleep(0.15)
        dex_data = fetch_dexscreener_data(mint) or {}
        report = fetch_rugcheck_report(mint)
        holders = get_non_escrow_holders(report, coin) if report else None
        tags = build_highlight_tags(
            has_twitter=bool((coin.get("twitter") or "").strip()),
            volume_m5=dex_data.get("volume_m5"),
            buys_m5=dex_data.get("buys_m5"),
            sells_m5=dex_data.get("sells_m5"),
            holder_count=(len(holders) if holders is not None else None),
        )
        tag_text = f"  [{' '.join(tags)}]" if tags else ""

        lines.append(f"{i}. ${esc(symbol)} — ${mcap:,.0f}{tag_text}")
        keyboard_rows.append([{"text": f"${symbol}", "callback_data": f"info:{mint}"}])
        top10_cache[mint] = {
            "coin": coin, "symbol": symbol, "name": coin.get("name", "?"),
            "cached_ts": time.time(),
        }
    lines.append("")
    lines.append("𝕏 X bağlantısı · 💰 hacim yüksek · 🔁 işlem çok · 👥 holder çok (varsa)")
    lines.append("👇 Detaylı (RugCheck dahil) rapor için bir coin'e dokun.")

    send_telegram_message_with_keyboard("\n".join(lines), keyboard_rows)
    print(f"[BİLGİ] Top10 (mcap sıralı) Telegram'a gönderildi: "
          f"{', '.join('$' + c.get('symbol', '?') for _, c in top10)}")


# --------------------------- FİYAT TAKİBİ (2-3x PATLAMA) ---------------------------

def track_new_coins_for_price_watch(coins, price_watch):
    """'En yeni 50' listesinde ilk kez gördüğümüz her coin için (güvenlik
    kontrolünden geçmiş geçmemiş FARK ETMEKSİZİN) baseline mcap kaydeder.
    Zaten takip edilen ya da mcap'i olmayan coinler atlanır."""
    for coin in coins:
        mint = coin.get("mint")
        if not mint or mint in price_watch:
            continue
        mcap = coin.get("usd_market_cap") or coin.get("market_cap") or 0
        if mcap <= 0:
            continue
        price_watch[mint] = {
            "symbol": coin.get("symbol", "?"),
            "name": coin.get("name", "?"),
            "first_mcap": mcap,
            "first_seen_ts": time.time(),
            "alerted_tiers": [],
            "pumpfun_url": f"https://pump.fun/coin/{mint}",
            "dexscreener_url": f"https://dexscreener.com/solana/{mint}",
        }


def send_price_spike_alert(mint, info, current_mcap, multiple, tier):
    """SADECE fiyat hareketine dayalı uyarı — mint/freeze/holder/rug GİBİ
    HİÇBİR GÜVENLİK KONTROLÜ YAPILMADI. Bu yüzden normal İZLE sinyaliyle
    KARIŞTIRILMAMASI için ayrı, açıkça uyarılı bir format kullanıyoruz."""
    symbol = info.get("symbol", "?")
    name = info.get("name", "?")
    text = (
        f"🚀 <b>{esc(symbol)}</b> (<i>{esc(name)}</i>) — <b>FİYAT PATLAMASI: {tier:.0f}x</b>\n"
        f"💰 ${info['first_mcap']:.0f} -> ${current_mcap:.0f}  (şu an %{(multiple - 1) * 100:.0f} yukarıda)\n\n"
        f"⚠️ Bu SADECE fiyat hareketine dayalı bir uyarı — mint/freeze authority, "
        f"holder dağılımı, rug riski gibi HİÇBİR güvenlik kontrolünden GEÇMEDİ. "
        f"Normal İZLE sinyalinden farklı, çok daha riskli bir bildirim; coin zaten "
        f"tepe yapmış ya da rug'un ortasında olabilir. Yatırım tavsiyesi değildir.\n\n"
        f'🔗 <a href="{info["pumpfun_url"]}">pump.fun</a> | '
        f'<a href="{info["dexscreener_url"]}">DexScreener</a>\n'
        f"🧾 <code>{esc(mint)}</code>"
    )
    send_telegram_message(text)


def process_price_watch(price_watch, coins_by_mint, recent_evals=None, winner_profiles=None):
    """PRICE_WATCH_CHECK_INTERVAL_MINUTES'te bir çağrılır (çağıran taraf
    zamanlamayı kontrol eder). Her takip edilen mint için mcap'i tazeler
    (önce o anki 50 listesinden, orada yoksa DexScreener'dan — ekstra
    maliyeti sadece listeden düşmüş coinlere sınırlamak için), tier'ları
    kontrol eder, süresi dolanları listeden düşürür. Bir mint İLK KEZ 2x+
    yaptığında ve daha önce tam değerlendirilmişse (recent_evals'te varsa),
    o ilk-bakış özelliklerini 'kazanan profili' olarak kalıcı listeye ekler
    (bkz. promote_to_winner_profile)."""
    now_ts = time.time()
    for mint in list(price_watch.keys()):
        info = price_watch[mint]
        age_min = (now_ts - info["first_seen_ts"]) / 60
        if age_min > PRICE_WATCH_MAX_AGE_MINUTES:
            del price_watch[mint]
            continue

        fresh_coin = coins_by_mint.get(mint)
        if fresh_coin is not None:
            current_mcap = fresh_coin.get("usd_market_cap") or fresh_coin.get("market_cap") or 0
        else:
            time.sleep(0.15)
            dex_data = fetch_dexscreener_data(mint)
            current_mcap = (dex_data or {}).get("mcap") or 0

        if not current_mcap or current_mcap <= 0:
            continue

        multiple = current_mcap / info["first_mcap"]
        alerted = set(info.get("alerted_tiers", []))
        was_empty = not alerted
        for tier in PRICE_WATCH_MULTIPLIER_TIERS:
            if multiple >= tier and tier not in alerted:
                send_price_spike_alert(mint, info, current_mcap, multiple, tier)
                alerted.add(tier)
        info["alerted_tiers"] = sorted(alerted)

        if was_empty and alerted and recent_evals is not None and winner_profiles is not None:
            promote_to_winner_profile(winner_profiles, recent_evals, mint, info.get("symbol", "?"), min(alerted))

        # En yüksek tier'a (10x) ulaşıp bunu da bildirdiysek artık izlemeye
        # gerek yok — sonsuza kadar DexScreener sorgulamayalım.
        if alerted and max(alerted) >= PRICE_WATCH_MULTIPLIER_TIERS[-1]:
            del price_watch[mint]


def process_telegram_callbacks(top10_cache, state):
    """Top10 mesajındaki butonlardan birine basılıp basılmadığını kontrol
    eder (Telegram'ın getUpdates'i ile — webhook/ayrı sunucu GEREKMEZ).
    Basılan coin için TAM değerlendirme (RugCheck dahil) o an yapılıp ayrı
    bir mesaj olarak gönderilir."""
    offset = state.get("telegram_update_offset", 0)
    updates = fetch_telegram_updates(offset)
    for upd in updates:
        offset = max(offset, upd.get("update_id", 0) + 1)

        msg = upd.get("message")
        if msg:
            try:
                handle_manual_mint_lookup(msg)
            except Exception as e:
                print(f"[HATA] manuel mint sorgusu işlenemedi: {e}")
            continue

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


def route_first_look_result(result, coin, pending, seen, recent_evals=None, winner_profiles=None):
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

    # --- "Benzer profil" öğrenme katmanı: bu ilk-bakış anlık görüntüsünü
    # kaydet (coin sonradan patlarsa bir 'kazanan profili'ne dönüşebilir),
    # ve KALICI GÜVENLİK VETOSU yoksa, bu coin'in geçmişte patlayan
    # coinlere ne kadar benzediğine bak. ---
    if recent_evals is not None:
        record_recent_eval(recent_evals, mint, result)
    if winner_profiles is not None and not (result["hard_vetoes"] and result.get("has_permanent_veto")):
        match = find_similar_winner_profile(result, winner_profiles)
        if match:
            profile, ratio = match
            send_similar_profile_alert(result, profile, ratio, len(winner_profiles))

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
            "recheck_attempts": 0,  # GROKLER dersi: iyileşebilir başarısızlıklara ek şans için
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


def process_young_watch(pending, seen, coins_by_mint=None, recent_evals=None, winner_profiles=None):
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
        route_first_look_result(result, fresh_coin, pending, seen, recent_evals, winner_profiles)

    return matured


def process_clean_watch_rechecks(pending, seen, coins_by_mint=None):
    """'clean_watch' aşamasındaki (ilk tam bakışta temiz + skor yeterli çıkmış)
    adayları, süresi dolduysa ikinci kez (momentum kontrolüyle) değerlendirir.
    Onaylanan (gerçekten büyüyen) adayların listesini döner. pump.fun'ın
    tekil coin API'si artık JWT gerektirdiği için ilk görüldüğünde saklanan
    coin objesi (info['coin']) + DexScreener'dan tazelenen mcap kullanılır;
    coin bu turun en-yeni-50 listesinde hâlâ varsa (nadir, çünkü bu aşamadaki
    coin'ler zaten birkaç dakikalık) o taze veri öncelikli kullanılır.

    ÖNEMLİ (GROKLER'in İKİNCİ hatası): burada kalıcı olarak GEÇ denen bir
    mint MUTLAKA `seen`'e eklenmeli. Eskiden sadece `pending`den siliniyordu;
    `seen`de olmadığı için coin hâlâ pump.fun'ın 'en yeni 50' listesindeyse
    bir SONRAKİ tarama turunda SIFIRDAN yeni bir aday gibi tekrar
    değerlendiriliyor, tekrar clean_watch'a girip tekrar başarısız olup
    İKİNCİ bir (mükerrer) erken-sinyal/iptal çifti gönderebiliyordu."""
    coins_by_mint = coins_by_mint or {}
    now_ts = time.time()
    finalized = []
    for mint in list(pending.keys()):
        info = pending[mint]
        if info.get("stage") != "clean_watch":
            continue
        elapsed_min = (now_ts - info["first_seen_ts"]) / 60
        next_check_ts = info.get("next_check_ts") or (info["first_seen_ts"] + RECHECK_DELAY_MINUTES * 60)
        if now_ts < next_check_ts:
            continue  # henüz sırası gelmedi

        stored_coin = info.get("coin")
        if stored_coin is None:
            log_geç(info.get("symbol", "?"), "-", 1, "coin verisi saklanmamış (eski format kaydı)")
            del pending[mint]
            seen.add(mint)
            mark_seen(mint)
            continue

        fresh_coin = dict(coins_by_mint.get(mint, stored_coin))
        time.sleep(0.2)  # DexScreener'a ard arda hızlı istek atmamak için
        apply_dexscreener_data(fresh_coin, mint)

        result = evaluate_candidate(fresh_coin)
        if result is None:
            log_geç(info.get("symbol", "?"), "-", 1, "ikinci bakışta yaş penceresi dışına çıktı")
            del pending[mint]
            seen.add(mint)
            mark_seen(mint)
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

        attempts = info.get("recheck_attempts", 0)
        has_permanent = bool(result["hard_vetoes"]) and (
            any(is_permanent_veto(v) for v in result["hard_vetoes"]) or dev_sold_note is not None
        )
        momentum_ok = mcap_growth_pct >= MIN_MCAP_GROWTH_PCT and (
            holder_growth is not None and holder_growth >= MIN_HOLDER_GROWTH
        )

        if momentum_ok and not result["hard_vetoes"]:
            result["decision"] = "İZLE (manuel doğrulama şart, momentum onaylı)"
            # İZLE'den SONRA da dev cüzdanını izlemeye devam edebilmek için ham
            # coin objesini ve bu andaki creator miktarını saklıyoruz (bkz.
            # process_post_confirm_watch / "İZLE sonrası takip").
            result["_raw_coin"] = fresh_coin
            finalized.append(result)
            del pending[mint]
        elif not has_permanent and attempts < CLEAN_WATCH_MAX_EXTRA_CHECKS:
            # --- GROKLER dersi: iyileşebilir bir başarısızlık (mcap/holder
            # geçici düşük çıktı ya da büyüme henüz yetersiz) tek bakışta
            # KALICI GEÇ saymıyoruz — coin oynak olabilir, bir dip'in ortasında
            # yakalamış olabiliriz. Sessizce ek bir tur daha bekletiyoruz,
            # henüz retraction GÖNDERMİYORUZ (kullanıcıyı gereksiz "iptal"
            # mesajıyla yorup sonra "aslında devam ediyormuş" demek istemiyoruz).
            reason = result["hard_vetoes"][0] if result["hard_vetoes"] else f"momentum yetersiz ({momentum_note})"
            info["recheck_attempts"] = attempts + 1
            info["next_check_ts"] = now_ts + RECHECK_DELAY_MINUTES * 60
            info["coin"] = fresh_coin
            print(f"[TEKRAR BEKLET-MOMENTUM] {result['symbol']} | deneme {attempts + 1}/{CLEAN_WATCH_MAX_EXTRA_CHECKS} | "
                  f"henüz kalıcı elenmedi, {RECHECK_DELAY_MINUTES} dk sonra tekrar bakılacak: {reason}")
        else:
            reason = result["hard_vetoes"][0] if result["hard_vetoes"] else f"momentum yetersiz ({momentum_note})"
            result["decision"] = "GEÇ"
            log_geç(result["symbol"], result["score"], len(result["hard_vetoes"]) or 1, reason)
            # Daha önce erken sinyal göndermiştik (bkz. send_early_watch_notification) —
            # şimdi o sinyalin geçersiz olduğunu bildiriyoruz (kalıcı veto ya da
            # ek şanslar tükendi).
            send_early_watch_retraction(result["symbol"], result["name"], reason)
            del pending[mint]
            seen.add(mint)
            mark_seen(mint)

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


def run_once(seen, pending, top10_cache, top10_state, price_watch, recent_evals, winner_profiles):
    # Listeyi en başta çekiyoruz ki AŞAMA A/B'deki adaylar, bu turun en-yeni-50
    # listesinde hâlâ varsa (nadir ama olası) DexScreener'a gitmeden önce en
    # taze pump.fun verisini kullanabilsin.
    coins = fetch_pumpfun_new_coins()
    coins_by_mint = {c.get("mint"): c for c in coins if c.get("mint")}

    # --- AŞAMA A: 'young' (ham, henüz sorgulanmamış) adaylardan olgunlaşanları
    # tam değerlendir (RugCheck dahil) ---
    matured = process_young_watch(pending, seen, coins_by_mint, recent_evals, winner_profiles)

    # --- AŞAMA B: 'clean_watch' (ilk bakışta temiz) adaylardan süresi dolanları
    # momentum onayıyla kesinleştir ---
    confirmed = process_clean_watch_rechecks(pending, seen, coins_by_mint)
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
        route_first_look_result(result, coin, pending, seen, recent_evals, winner_profiles)

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

    # --- AŞAMA F: Fiyat takibi (2-3x patlama uyarısı) ---
    # Güvenlik pipeline'ından BAĞIMSIZ: her yeni görülen coin'in mcap'i
    # baseline olarak kaydedilir (ucuz, ekstra istek yok), PRICE_WATCH_
    # CHECK_INTERVAL_MINUTES'te bir de takip edilenlerin mcap'i tazelenip
    # belirlenen katlara (2x/3x/5x/10x) ulaşan varsa uyarı gönderilir.
    track_new_coins_for_price_watch(coins, price_watch)
    last_price_watch_ts = top10_state.get("last_price_watch_check_ts", 0)
    if now_ts - last_price_watch_ts >= PRICE_WATCH_CHECK_INTERVAL_MINUTES * 60:
        process_price_watch(price_watch, coins_by_mint, recent_evals, winner_profiles)
        top10_state["last_price_watch_check_ts"] = now_ts
        save_top10_state(top10_state)
    save_price_watch(price_watch)

    # --- AŞAMA G: "Benzer profil" öğrenme katmanı bakımı ---
    prune_recent_evals(recent_evals)
    save_recent_evals(recent_evals)
    save_winner_profiles(winner_profiles)


def main():
    print("DESK Scanner v6 (üç aşamalı: genç takip -> tam değerlendirme -> momentum onayı, "
          "+ market cap sıralı Top10 yayını + fiyat patlaması takibi + benzer profil öğrenimi) "
          "başladı. Ctrl+C ile durdur.")
    # Telegram env değişkenleri eksikse, bot ÇÖKMÜYOR ve TÜM Telegram
    # fonksiyonları sessizce hiçbir şey yapmıyor (send_telegram_message vb.
    # başında "if not TOKEN or not CHAT_ID: return" var) -- yani mesaj
    # gelmemesinin en sık sebeplerinden biri, hiçbir hata basmadan, budur.
    # Bunu net görebilmek için başlangıçta açıkça uyarıyoruz.
    if not TELEGRAM_BOT_TOKEN:
        print("[UYARI] TELEGRAM_BOT_TOKEN ayarlanmamış -> Telegram'a HİÇBİR mesaj gönderilmeyecek "
              "(Railway -> Variables kısmını kontrol et).")
    if not TELEGRAM_CHAT_ID:
        print("[UYARI] TELEGRAM_CHAT_ID ayarlanmamış -> Telegram'a HİÇBİR mesaj gönderilmeyecek "
              "(Railway -> Variables kısmını kontrol et).")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        test_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe"
        try:
            resp = requests.get(test_url, timeout=10)
            if resp.status_code == 200 and resp.json().get("ok"):
                bot_username = resp.json().get("result", {}).get("username", "?")
                print(f"[BİLGİ] Telegram bot bağlantısı OK -> @{bot_username}")
            else:
                print(f"[UYARI] Telegram bot token'ı geçersiz görünüyor (HTTP {resp.status_code}: "
                      f"{resp.text[:200]}) -> mesajlar gitmeyecek.")
        except Exception as e:
            print(f"[UYARI] Telegram'a başlangıç bağlantı testi başarısız: {type(e).__name__}: {e}")
    seen = load_seen()
    pending = load_pending()
    top10_cache = load_top10_cache()
    top10_state = load_top10_state()
    price_watch = load_price_watch()
    recent_evals = load_recent_evals()
    winner_profiles = load_winner_profiles()
    consecutive_errors = 0
    while True:
        try:
            run_once(seen, pending, top10_cache, top10_state, price_watch, recent_evals, winner_profiles)
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
