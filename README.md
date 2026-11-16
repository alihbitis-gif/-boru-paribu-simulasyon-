# BÖRÜ 2.0 — Paribu sanal işlem denemesi

**20.000 TL sanal sermaye; net −%2 zarar kes, +%3 kâr al.** Yalnız genel piyasa verisi okunur.
Gerçek emir, hesap anahtarı veya para transferi yoktur. Bu sürümün kârlılığı henüz doğrulanmamıştır.

## Neler düzeltildi?

| Alan | 2.0 davranışı |
|---|---|
| Komisyon | Her yönde %0,28 standart TL taker varsayımı; alışta coin, satışta TL kesintisi |
| Alış sinyali | Orta fiyat yerine alış teklifinin (bid) kapanışı ve zirvesi; en az %0,1 kırılım tamponu |
| Teyit | Son işlem fiyatında yeni gözlenen değişim, kırılım seviyesinin korunması ve 5 dakikalık eğilim |
| Piyasa yönü | Altcoin alışlarında güncel BTC/TL verisi ve pozitif 5 dakikalık eğilim |
| Sermaye | İşlem başına %1, toplam %2 risk bütçesi; %0,5 fiyat kayması payı |
| İşlem maliyeti | Komisyon + makas + derinlik etkisiyle hemen geri satış kaybı en fazla %0,8 |
| Süre sonu | Yeni alışlar kapanır; açık pozisyon takibi ayrıca sürer |
| Kısmi stop | Stop tetiklenince kalan miktarın çıkış isteği korunur; aynı defter tekrar tüketilmez |
| Kayıt | Alış/satış/eleme gerekçesi, tüm alınan defter kademeleri, ticker örnekleri, kullanılan komisyon |
| Rapor | Brüt miktar/net alınan miktar ve komisyon varlığı ayrı; eski raporlar kendi varsayımını korur |

Bunlar deneme parametreleridir; en iyi veya kâr garantili ayarlar oldukları iddia edilmez.
Başlangıçta tek pozisyon bütçesi `20.000 × %1 / (%2 + %0,5) = 8.000 TL`, toplam maliyet sınırı
aynı hesapla 16.000 TL'dir. Likidite yetersizse daha az alınabilir; uygun fırsat yoksa nakitte kalınır.
Risk bütçesi fiyat boşluğunda veya veri kesintisinde zarar için kesin bir tavan değildir.

## Veri ve strateji sınırları

- Yalnız `GET https://api.paribu.com/market/ticker` ve `GET /orderbook` kullanılır.
- 24 saatlik TL hacmi 5 milyon TL üzerindeki ilk 10 TL paritesi ve BTC/TL gözlenir.
  USDT/TL ve USDC/TL alış adayı olmaz. Açık pozisyonlar liste dışına çıksa da takip edilir.
- Her 15 saniyelik defter örneğinden bid fiyat barları üretilir. Son işlem fiyatı ticker'dan ayrıca örneklenir.
  **Bunlar tüm gerçekleşen işlemleri içeren OHLCV ya da dakika hacmi değildir.** 24 saatlik hacim
  farkı dakika hacmi gibi kullanılmaz. İşlem/hacim akışı doğrulanmadan hacim teyidi var denmez.
- En az 60 ardışık kapanmış dakika, her dakikada iki ayrı defter zamanı gerekir. Açık mum kullanılmaz.
- EMA10 > EMA30; basit RSI14 50–75; pozitif 5 dakikalık momentum; dakika oynaklığı %0,01–%0,8.
- Bid kapanışı önceki 20 dakikanın en yükseğini `max(%0,1, oynaklık/2)` kadar geçmelidir.
  Güncel bid ve örneklenen son işlem fiyatı da bu seviyeyi korumalıdır. Son kapanmış dakikanın
  başlangıcından itibaren en az iki ticker örneği ve en az bir fiyat değişimi bulunmalıdır.
- Tamamlanmış 5 dakikalık bid barlarında EMA3 > EMA8 ve son kapanış ≥ önceki kapanış aranır.
  Altcoinler için aynı kontrol güncel BTC/TL üzerinde de yapılır. Eksik/eskimiş teyit alış açmaz.
- Makas en fazla %0,3; alışın kademe fiyat etkisi en fazla %0,3. Görünür miktarın en fazla %10'u kullanılır.
  En az 1.000 TL alış, en fazla üç pozisyon ve satıştan sonra 30 dakika bekleme kuralı vardır.
- Tam çıkış derinliği bilinmeyen açık pozisyon varsa portföy değeri `null`; yeni risk alınmaz.
- 30 saniyeden eski defterle alış/satış yapılmaz. Eksik veri sırasında stop çalışmış sayılmaz.
- Eski orta fiyat sinyali `shadow_signals` içinde aynı gözlemlerle karşılaştırma için kaydedilir.
  Bu yalnız sinyal karşılaştırmasıdır; ikinci bir portföy veya kâr karşılaştırması değildir.

Komisyon kaynağı (16.11.2026 kontrolü):
https://www.paribu.com/destek/alim-satim/komisyon-maker-taker
Standart 1. seviye TL taker %0,28'dir. Kişisel hacim seviyesi/Loyalty indirimi bilinmediği için varsayılır;
oturum içinde kademe indirimi hesaplanmaz. Ayar `Config` içinde ve oturum veritabanında saklanır.
Aynı oturumun oranı sessizce değiştirilemez.

## Çalıştırma ve süre yönetimi

Python 3.10+, Linux/macOS ve grafik için `matplotlib==3.10.8` kullanılır.

```sh
python -m pip install matplotlib==3.10.8
python -m unittest -v
python -u boru.py --directory data-v2 --duration-minutes 180 --max-monitor-minutes 120 --interval 15 --require-data
```

Bu komut üç saat yeni fırsat izler. Süre sonunda pozisyon varsa **yalnız çıkış takibi** en fazla iki saat
sürer. Pozisyon kapanınca biter. İki saatlik ek sınır da dolarsa sahte satış yapılmaz:
`session_end.json` içinde `PAUSED_OPEN_POSITIONS`, `requires_resume: true`, `monitoring_active: false`
yazılır; artık çalışan stop takibi yoktur. İşletim sistemi süreci zorla öldürürse son kontrol noktası kullanılır.

Sürekli worker için `--duration-minutes 0` kullanılabilir; kalıcı disk ve tek süreç gereklidir.
Bu kod değişikliği tek başına 7/24 sunucu kurulumu değildir. İkinci süreç aynı dizini açamaz.

Aynı v2 diziniyle yeniden başlatma bakiyeyi veya alış penceresini sıfırlamaz. Açık pozisyonlu duraklamayı
sürdürmek için indirilen kontrol noktası tek bir veri dizinine konur ve örneğin:

```sh
python -u boru.py --directory restored-v2 --resume-monitor-minutes 120 --interval 15
```

Bu komut yalnız v2 oturumunun bitmiş alış penceresi için izleme süresini uzatır; yeni alışları açmaz.
1.0 veritabanı 2.0 ile işlem yapmak üzere açılmaz. Önceki sonuçlar değişmeden raporlanabilir.

## GitHub Actions ve raporlar

- Olağan kod değişikliği yeni simülasyon başlatmaz. PR senkronizasyonundaki **yalnız yeni değişiklik**
  `.github/start-paper-session` dosyasını içeriyorsa başlatılır; PR'nin birikimli dosya listesine güvenilmez.
- `BORU live paper simulation` yeni oturumda önce testleri ve üç gözlemi kontrol eder.
  Sonra yukarıdaki 180 + en çok 120 dakikalık deneme çalışır; sınır 320 dakikadır.
- Her beş dakikada tutarlı SQLite yedeği `boru-checkpoint-*` çıktısına kaydedilir; son iki yedek tutulur.
  Son işlem defteri `boru-session-results` çıktısındadır. Çıktıların saklama süresi 30 gündür.
- Kayıtlar oturum boyunca silinmez. Sürekli sunucuda disk kapasitesi ve arşivleme ayrıca yönetilmelidir.
- Her alış/kısmi veya tam satış için ayrı PNG, JSON, Markdown hazırlanır. `index.json` bütün raporları listeler.
  Rapor; fiyat gözlemleri, işlem noktaları, net −%2/+%3 referansları ve gerçekleşmiş kâr/zararı gösterir.
- Yükleme veya olay yazma başarısızsa yeni işlem beklenmeden sonraki gözlemlerde yeniden denenir.
  `report_delivery.json` yükleme/olay durumunu gösterir; ChatGPT teslimi doğrulanmış sayılmaz.
- PR #1'e teknik rapor-hazır olayı yazılması mevcut bildirim akışını uyandırır. Bildirim anlık garanti edilmez.
  Tekrarlar rapor kimliğiyle elenir; her rapor kendi komisyonunu ve sürümünü taşır.
- İlk `43849993630` oturumu 16.11.2026'da tamamlandı; raporları **oturum sonunda** topluca üretildi.
  Eski %0,35 maliyet modeli ve gerçekleşen işlemleri yeniden fiyatlanmaz.

```sh
python reporting.py --directory data-v2
```

Bir işlemden strateji başarısı çıkarılmaz. Yeni kuralların etkisi birden çok bağımsız oturumda;
net sonuç, maliyet, düşüş, elenen sinyaller ve veri kesintileri birlikte incelenerek ölçülmelidir.
