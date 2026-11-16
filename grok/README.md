# BÖRÜ Grok kurulumu — 3 bot, 4 ajan

Bu paket üç mantıksal botun görevlerini ve xAI API üzerinden dört ajan rolünü içerir.
Grok Bot uygulamasında kullanıcı hesabına bot profili yaratıldığı anlamına gelmez.
Yerel kontrolde XAI_API_KEY bulunmadığı için gerçek model çağrısı yapılmadı.
Sahte/test ajan yanıtları canlı sonuç olarak yayımlanmaz.

| Bot | Ajan | Prompt |
|---|---|---|
| BÖRÜ Analiz | Teknik analiz + zincir/büyük işlem | common.txt + technical.txt / chain.txt |
| BÖRÜ Karar | Portföy değerlendirme | common.txt + decision.txt |
| BÖRÜ Rapor | İşlem açıklaması | common.txt + report.txt |

## Hazır çalışan parçalar

- `chains.py`: altı mainnet için ağ kimliği, blok zamanı ve onay durumu doğrulaması.
- `market_intel.py`: ayrı iki iş parçacığıyla zincir örneklemesi; mevcut piyasa araştırmasını ve
  risk döngüsünü ağ istekleriyle bekletmez. SQLite kayıtları ana süreçte yapılır.
- `grok_team.py`: iki analiz rolünü paralel, karar rolünü ardından çalıştırır. Rapor rolü
  doğrulanmış rapor paketi için ayrı çağrılır. JSON şeması, kaynak kimliği, paket kimliği,
  bütçe oranı ve süre denetlenir. Anahtar/servis hatasında kapalı kalır.
- Sanal muhasebe, emir defteri gerçekleşmesi ve acil çıkışlar mevcut `boru.py` motorundadır.
- Grafik üretimi, rapor arşivi ve tekrarsız teslim mevcut raporlama bileşenlerindedir.

## Bu sürümün sınırı

Grok çıktıları **ADVISORY_ONLY** olarak kaydedilir; muhasebeye veya gerçek emre yazma yetkisi
yoktur. API anahtarıyla canlı model davranışı ve gecikme doğrulanmadan bu görüşler işlem
motoruna bağlanmaz. Yalnız prompt yazılmış olması dört ajanın aktif çalıştığını göstermez.
Mevcut v3 oturumu eski commit ile devam eder; yeni kod o çalışan süreci değiştirmez.

## Bağlantı ve deneme

Python 3.10+; Grok istemcisi standart kütüphaneyi kullanır. Anahtarı sohbete veya depoya
koymayın; sunucunun gizli ortam değişkeni `XAI_API_KEY` olarak tanımlayın. Model ortam
değişkeni `BORU_GROK_MODEL`, varsayılan `grok-4.7`. Bu bir abonelik veya harcama taahhüdü
oluşturmaz; canlı çağrı ayrıca kimliği doğrulanmış hesap gerektirir.

```sh
python grok_team.py --check
python chains.py --output chain-data --require-all
python grok_team.py --directory EXISTING_SESSION_DIRECTORY --market btc_tl --output grok-review
```

Son komut mevcut SQLite defterini salt okunur açar. İnceleme başına üç model isteği yapar;
rapor açıklaması ayrı bir istektir. Haber/X araması ve kod çalıştırma aracı tanımlı değildir.
İlk kullanımda eski veri nedeniyle karar reddedilebilir; veri tarihi değiştirilerek aşılmaz.

Grok Bot uygulamasında hesabın içine girişten sonra üç isimle profil oluşturulur. Her birine
`common.txt` ve tabloda belirtilen görev metinleri verilir. Analiz botunun iki ajanı,
buradaki API koordinatörüyle ayrı rollerdir; uygulamanın sabit alt ajan ayarı olduğu iddia edilmez.

## Altı yazılım işçisi

1. Paribu veri toplayıcı (`boru.MarketData`).
2. Mum/gösterge motoru (`market_intel.PublicResearch`).
3. Altı ağ zincir toplayıcı (`chains.ChainMonitor`).
4. Kod tabanlı risk gözetmeni (`strategy`, `boru.Simulation`).
5. Tek yazarlı sanal gerçekleşme ve muhasebe (`boru.Simulation`).
6. Grafik/kayıt/teslim kontrolü (`reporting`, rapor yayıncısı ve mevcut webhook görevi).

Bunlar mantıksal görevlerdir; altı ayrı sunucu veya on sürekli model süreci değildir.

## Kaynaklar

- https://docs.x.ai/grok-bot/bots
- https://docs.x.ai/developers/tools/function-calling
- https://docs.x.ai/developers/model-capabilities/text/structured-outputs
- https://github.com/Blockstream/esplora/blob/master/API.md
- https://ethereum.org/en/developers/docs/apis/json-rpc/
- https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/
- https://solana.com/docs/rpc/http/getblock
- https://developers.tron.network/reference/eth_chainid
- https://developers.tron.network/docs/exchange-wallet-integration
- https://xrpl.org/docs/references/protocol/transactions/metadata
