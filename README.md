# BÖRÜ — Paribu canlı kâğıt işlem simülasyonu

Başlangıç **20.000 TL sanal bakiye**. Gerçek emir, hesap bağlantısı veya API anahtarı yoktur.
Bu ileriye dönük bir denemedir; geçmiş performans testi veya doğrulanmış kazanç stratejisi değildir.

## Çalıştırma

Python 3.10+ ve standart kütüphane yeterlidir.

```sh
python -m unittest -v
python -u boru.py --duration-minutes 180 --interval 15 --require-data
```

GitHub Actions testleri ve üç canlı gözlemi doğrular, ardından 180 dakikalık sunucu oturumu çalıştırır.
Bu oturum 7/24 hizmet değildir; süre sonunda durur. `boru-session-results` çıktısında SQLite işlem defteri,
`status.json` ve zaman damgalı `reports.jsonl` bulunur. Açık pozisyonlar oturum sonunda zorla kapatılmaz.
Yeni bağımsız Actions çalışması 20.000 TL ile yeni oturum açar; öncekinin üzerine eklenmez.

## İlk sürümün kuralları

- Paribu `GET /market/ticker` ve `GET /orderbook` genel uçları kullanılır; yalnız bu iki GET isteğine izin verilir.
- 24 saatlik TL hacmi en az 5 milyon TL olan en likit 10 TL paritesi izlenir; USDT/TL ve USDC/TL hariçtir.
- Her 15 saniyede emir defterinin alış/satış orta fiyatından 1 dakikalık gözlem barları oluşturulur.
  Bunlar işlem bazlı OHLCV değildir. Geçmiş mum veya dakika hacmi uydurulmaz.
- Alım için en az 60 ardışık kapanmış dakika ve her dakikada en az iki farklı zaman damgalı gözlem gerekir.
  İlk hazırlık yaklaşık 61 dakika sürer; veri boşlukları süreyi uzatabilir.
- EMA10 > EMA30, önceki 20 dakikanın gözlenen zirvesinin kırılması, RSI14 50–75,
  pozitif 5 dakikalık momentum ve dakika log getiri oynaklığı %0,01–%0,8 koşulları birlikte aranır.
  RSI son 14 fiyat değişiminin basit ortalamasını kullanır.
- Alış/satış farkı en fazla %0,3; alışın ek fiyat etkisi en fazla %0,3 olabilir.
- En fazla üç aday momentum/oynaklık puanları oranında kullanılabilir nakdi paylaşır.
  Tek aday bütün nakdi kullanabilir; uygun aday yoksa nakitte beklenir.
  Görünür emir miktarının yalnız %10'u kullanılabilir; alış alt sınırı 1.000 TL'dir.
- Her yön için **%0,35 komisyon varsayılır**. Bu kullanıcının doğrulanmış Paribu komisyon oranı değildir.
  Gerçek emir defteri kademeleri üzerinden ağırlıklı fiyat ve gerekirse kısmi işlem hesaplanır.
- Alış ve satış komisyonları sonrası tahmini net getiri **−%2 veya +%3** eşiğine ulaşınca satış denenir.
  Fiyat boşluğu, 15 saniyelik kontrol aralığı, gecikme ve likidite nedeniyle sanal sonuç eşikten farklı olabilir.
  Satış sonrası aynı üründe 30 dakika beklenir.
- 30 saniyeden eski veya hatalı emir defteriyle işlem yapılmaz. Fiyatlanamayan pozisyon varsa toplam değer `null` olur.
  Veri kesintisinde stop seviyesinin çalıştığı varsayılmaz. API hataları kaydedilir.
- Kararlar ve maliyetler işlem kayıtlarına, gözlemler SQLite'a yazılır.
  Aynı veri diziniyle yeniden başlatmak bakiyeyi sıfırlamaz.

## Kalıcı sunucu

Sürekli çalışma komutu: `python -u boru.py --duration-minutes 0 --interval 15`.
`BORU_DATA_DIR` kalıcı diskteki dizini göstermeli; tek çalışan süreç kullanılmalıdır.
Render üzerinde sürekli worker ve kalıcı disk ayrıca kurulmalıdır. Actions oturumu Render kurulumu anlamına gelmez.

## Veri belgeleri

- https://docs.paribu.com/api/market-data/ticker
- https://docs.paribu.com/api/market-data/orderbook

Bu ilk sürümün varsayımları gerçek ileriye dönük kayıtlarla değerlendirilecektir.
Az işlem, sıfır işlem veya kısa dönem kârı stratejinin başarısını kanıtlamaz.
