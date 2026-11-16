# BÖRÜ — v3 karar motoru, altı ağ bağlantısı ve Grok ekip paketi

20.000 TL sanal sermaye. Gerçek emir veya hesap anahtarı kullanılmaz. Haber, sosyal medya ve duyum işlenmez.
Bu sürüm test aşamasındadır; kârlılık veya gelecekteki fiyat yönü için doğrulanmış tahmin iddiası yoktur.

## Yeni altı ağ ve Grok paketi

Bitcoin, Ethereum, Solana, BNB Smart Chain, TRON ve XRP Ledger için salt okunur
mainnet adaptörleri eklendi. `python chains.py --output chain-data --require-all` her ağı
ayrı sınar ve başarısızlığı gizlemeden kaydeder. `market_intel` bu verileri arka planda
örnekler ve `chain_activity` olarak saklar. **Bu alan alış/satış yönü kanıtı değildir**;
mevcut iki Ethereum havuzunun takas verisinden ayrı tutulur. Transfer örnekleri −%2
altında beklemek için gereken takas teyidinin yerine geçmez.

Kapsam: Bitcoin'de altı teyitli bir bloğun ilk 25 işlemi; Ethereum/BNB'de bir finalized
bloğun başarılı üst seviye yerel coin değer aktarımları; Solana'da bir finalized slotun
başarılı System Program transferleri; TRON'da bir solidified bloğun başarılı TRX transferleri;
XRP'de bir validated defterdeki gerçekten teslim edilmiş yerel XRP ödemeleri.
İşlem filtreleri coin cinsindendir ve balina kimliği değildir. BTC para üstü bilinmez.
Her ağın bütün blokları, tokenları, DEX'leri veya şirket sahiplikleri taranmaz. Örnekler
arasındaki taranmayan yükseklik aralığı açıkça kaydedilir; Solana'da slot atlaması da bu
aralığa dahil olabilir. Son örneğin alınması geçmişte hiç olay kaçırılmadığını göstermez.

Grok paketi: **3 mantıksal bot, 4 ajan rolü**, iki paralel analiz ve ardından karar incelemesi.
Kurulum ve görev metinleri [grok/README.md](grok/README.md) içindedir. `grok_team.py --check`
anahtar bulunup bulunmadığını değerini açıklamadan gösterir. İlk sürüm **ADVISORY_ONLY**;
gerçek Grok erişimi doğrulanmadan işlem yetkisi verilmez. Grok Bot uygulamasında profil
oluşturulması ayrıca kullanıcı hesabına giriş gerektirir. Mevcut çalışan oturum eski
commit ile devam eder; bu kod değişikliği onu yeniden başlatmaz ve yeni simülasyon açmaz.

## Güncel kapsam

| Veri | Kaynak ve uygulama | Sınır |
|---|---|---|
| Tüm Paribu TL pariteleri | Genel ticker, hedef 15 saniyelik döngü | Ağ gecikmesi / API hatası döngüyü uzatabilir; WebSocket veya sıfır gecikme iddiası yok |
| Paribu emir defteri | Açık pozisyonlar, güçlü adaylar ve dönen örneklem | Her coinin derinliği aynı anda indirilmez; işlemden önce güncel defter gerekir |
| Gerçek mumlar | Binance'te eşleşen USDT paritesinin saatlik ve günlük OHLCV'si | Paribu TL mumu değildir; eşleşmeyen veya kimliği onaylanmamış coin işlem açmaz |
| 1 saat / 24 saat / 7 gün | En az 168 kapalı saatlik mum ve 8 günlük mumdan hesaplanır | Yeni listeleme, eksik veya eski veri alışa izin vermez |
| Büyük borsa işlemleri | Binance gerçekleşmiş aggregate trades, alış/satış taker yönü | En son 1.000 kayıt; 5 dakikanın tamamını kapsamayabilir; cüzdan/kişi kimliği bilinmez |
| Zincir üstü büyük işlemler | Ethereum, iki doğrulanmış Uniswap v3 ETH/USDC havuzu | Bütün zincirler veya bütün balinalar değildir; 12 blok teyit gecikmesi vardır |
| Şirket ve ortak cüzdanları | Doğrulanmamış olarak raporlanır | Sahiplik tahmin edilmez; özel borsa hesapları ve tüm ortak işlemleri görülemez |

Eski Paribu `chart/history` ve `latest-matches` adresleri 16.11.2026 bağlantı kontrolünde 404 döndü.
Bu nedenle başka borsanın mumları Paribu geçmişi gibi sunulmaz. Tüm TL fiyatları izlenirken,
arka planda en fazla dört araştırma işi yürür. İlk kapsam taraması coin sayısına göre zaman alır.
Açık pozisyon araştırması hedef 60 saniyede, diğer varlıklar hedef 5 dakikada yenilenir;
kuyruk ve veri sağlayıcı sınırları bu süreleri uzatabilir. Her karar veri yaşı ve kapsama dayanır.

## Alış ve satış mantığı

- Mumlar: bullish/bearish engulfing, hammer, shooting star, doji; saatlik EMA10/30,
  günlük EMA3/7, ATR, hacim oranı, destek/direnç ve üç zaman penceresinin getirisi hesaplanır.
- Büyük satış gözlemi **tek başına alış değildir**. Yerel fiyat toparlanması ve mum/hacim teyidiyle
  karşı yönde alış adayı olur. Büyük alış gözlemi eldeki net kârlı pozisyonda satış nedeni olabilir.
- Borsa işlemlerindeki yön, agresif emrin (taker) yönüdür. Büyük emir vermek kimliği doğrulanmış
  bir balina olmak demek değildir. Borsa akışı ile zincir akışı tek bir küresel toplam gibi toplanmaz.
- Zincirde sıradan token transferi alış/satış sayılmaz. Doğrulanmış havuzun Swap olayı çözülür;
  silinmiş/reorg işareti taşıyan olaylar reddedilir. Şirket veya cüzdan sahibi uydurulmaz.
- **−%2 ve +%3 karar inceleme seviyeleridir; 3.0'da sabit satış kuralları değildir.**
  Güçlü saatlik/günlük eğilim + güncel zincir teyidi varsa −%2 altında toparlanma için bekleyebilir.
  Teyit yoksa kaybı taşımaz. Fiyat/eğilim bozulmasında −%2'yi beklemeden çıkabilir.
- Kârda zayıflama veya büyük alış gözlemi varsa +%3 beklenmez. Yükseliş desteği sürerse +%3 üzerinde tutar.
  Görülen net kâr zirvesinden ATR'ye göre %0,6–%1,8 geri çekilme kâr koruma çıkışını tetikler.
- Deneysel **net −%5 acil risk sınırı** ayrıca uygulanır. Sınır, kaymanın olmayacağı garantisi değildir.
  Veri yokken işlem olmuş gibi yazılmaz; yeniden güncel defter bulunduğunda gerçek görülen kademeler kullanılır.
- Veri desteği mantıksal olarak değerlendirilir; kesin yükseliş ihtimali veya uydurma olasılık yüzdesi üretilmez.

## Maliyet ve sermaye

Standart TL taker komisyonu her yön için %0,28 varsayılır; kişisel oran doğrulanmamıştır.
Alış komisyonu alınan coinden, satış komisyonu TL'den kesilir. Alış komisyonunun TL karşılığı
nakitten ikinci kez düşülmez. Eski 1.0 raporları kendi %0,35 varsayımını korur.

İşlem başına portföyün %1'i, toplam %2'si risk bütçesidir. 3.0 sermaye hesabında −%5 sınırına
%0,5 kayma payı eklenir: başlangıçta tek pozisyon üst bütçesi yaklaşık 3.636 TL,
toplam maliyet üst bütçesi yaklaşık 7.273 TL olur. Daha az kullanabilir veya nakitte kalabilir.
En fazla üç pozisyon; en az 1.000 TL alış; görünür miktarın en fazla %10'u; makas ve alış kademe
etkisi ayrı ayrı en fazla %0,3; anında geri satış maliyeti en fazla %0,8.
Bu deneme limitleri piyasa riski için kesin zarar tavanı değildir.

## Çalıştırma ve süre yönetimi

Python 3.10+, Linux/macOS ve grafik için `matplotlib==3.10.8` kullanılır.

```sh
python -m pip install matplotlib==3.10.8
python -m unittest -v
python -u boru.py --directory data-v3 --duration-minutes 180 --max-monitor-minutes 120 --interval 15 --require-data
```

Bu komut üç saat yeni fırsat izler. Süre sonunda pozisyon varsa **yalnız çıkış takibi** en fazla iki saat
sürer. Pozisyon kapanınca biter. İki saatlik ek sınır da dolarsa sahte satış yapılmaz:
`session_end.json` içinde `PAUSED_OPEN_POSITIONS`, `requires_resume: true`, `monitoring_active: false`
yazılır; artık çalışan stop takibi yoktur. İşletim sistemi süreci zorla öldürürse son kontrol noktası kullanılır.

Sürekli worker için `--duration-minutes 0` kullanılabilir; kalıcı disk ve tek süreç gereklidir.
Bu kod değişikliği tek başına 7/24 sunucu kurulumu değildir. İkinci süreç aynı dizini açamaz.

Aynı aynı sürümdeki dizinle yeniden başlatma bakiyeyi veya alış penceresini sıfırlamaz. Açık pozisyonlu duraklamayı
sürdürmek için indirilen kontrol noktası tek bir veri dizinine konur ve örneğin:

```sh
python -u boru.py --directory restored-v3 --resume-monitor-minutes 120 --interval 15
```

Bu komut yalnız oturumun bitmiş alış penceresi için izleme süresini uzatır; yeni alışları açmaz.
1.0 veritabanı yeni sürümle işlem yapmak üzere açılmaz. Önceki sonuçlar değişmeden raporlanabilir.


## Kayıt, grafik ve yayın

- `candles`: kaynak borsa ve zaman aralığıyla gerçek kapanmış OHLCV.
- `research_snapshots`: göstergeler, büyük işlemler, zincir kapsamı ve veri hataları.
- `execution_context`: her alış/satış kararında kullanılan araştırma ve strateji sürümü.
- `books`, `ticker_observations`, `decisions`: alınan defterin kademeleri, tüm TL fiyat örnekleri,
  yapılan ve elenen işlemlerin gerekçeleri. Veri kesintisi sıfır akış diye gösterilmez.
- 3.0 grafiklerinde Paribu TL fiyatları ve Binance USDT mumları ayrı panellerdedir.
  −%2/+%3 çizgileri inceleme referansı olarak etiketlenir. Gerçekleşmiş kâr/zarar ayrı çizilir.
- Her işlem sonrası PNG/JSON/Markdown; yükleme hatalarında yeni işlem beklemeden tekrar deneme.
  PR #1 rapor-hazır olayı bildirim otomasyonunu uyandırır. Teslim ve anlık bildirim garanti edilmez.
- Her beş dakikada tutarlı SQLite kontrol noktası; son iki yedek, 30 gün Actions saklama süresi.
  `boru-session-results` son defteri saklar. Sürekli sunucuda disk arşivleme ayrıca gereklidir.
- Olağan kod değişikliği yeni simülasyon açmaz. Yeni oturum yalnız açık bir başlatma isteğiyle
  `.github/start-paper-session` değişikliği veya workflow_dispatch üzerinden açılır.
- 1.0 kaydı değişmez; 2.0 kayıtları kendi sabit eşik modeliyle devam eder. Yeni 3.0 için ayrı oturum gerekir.
- Bu kurulum 7/24 hizmet değildir. Actions denemesi en çok 180 dakika giriş + 120 dakika çıkış takibidir.

## Birincil kaynaklar

- Paribu komisyon: https://www.paribu.com/destek/alim-satim/komisyon-maker-taker
- Binance piyasa API: https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints
- Binance yalnız piyasa verisi sunucusu: https://developers.binance.com/en/docs/products/spot/rest-api
- Ethereum JSON-RPC: https://ethereum.org/en/developers/docs/apis/json-rpc/
- PublicNode Ethereum: https://ethereum.publicnode.com/
- Uniswap v3 Swap ABI: https://github.com/Uniswap/v3-core/blob/main/contracts/interfaces/pool/IUniswapV3PoolEvents.sol
- USDC resmi sözleşme: https://developers.circle.com/stablecoins/usdc-contract-addresses

`python -m unittest -v` sentetik testleri, `python probe_feeds.py` ise yalnız gerçek veri bağlantılarını
sınar. Test verileri canlı oturum sonucu olarak yayınlanmaz. Kapsanmayan coin/zincir/şirket bilgisi
kapsanıyormuş gibi gösterilmez; veri bağlantısı başarısızlığı işlem yetkisini genişletmez.
