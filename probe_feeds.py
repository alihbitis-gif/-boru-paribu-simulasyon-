"""Contract checks using real public feeds; no simulation/account/order is created."""
import json
from market_intel import PublicResearch

if __name__=='__main__':
    provider=PublicResearch()
    market=provider.analyze('btc_tl')
    assert market['status']=='READY' and len(market['hours'])>=168 and len(market['days'])>=8
    print(json.dumps({'source':'Binance public market data','status':market['status'],
        'hourly_candles':len(market['hours']),'daily_candles':len(market['days']),
        'trade_feed_fresh':market['trade_feed_fresh'],'company_ownership':market['company_ownership']}),flush=True)
    chain=provider.chain()
    assert chain['status']=='READY' and len(chain['pools'])>0
    print(json.dumps({'source':'Ethereum public RPC','status':chain['status'],
        'verified_pools':chain['pools'],'confirmed_through':chain['confirmed_through'],
        'confirmations':chain['confirmations'],'events':len(chain['events']),
        'scope':chain['scope']}),flush=True)
