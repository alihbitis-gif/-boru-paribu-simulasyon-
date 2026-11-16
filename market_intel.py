"""Read-only public market research. Coverage is explicit; no news or private accounts."""
import concurrent.futures
import json
import math
import re
import threading
import time
import urllib.parse
import urllib.request

BINANCE = 'https://data-api.binance.vision'
RPC = 'https://ethereum-rpc.publicnode.com'
# Explicit market identities. Other exact-symbol matches remain diagnostic only.
VERIFIED_BASES = frozenset('BTC ETH SOL XRP ADA DOGE AVAX DOT LINK LTC BCH TRX BNB ATOM UNI AAVE XLM ALGO FIL ICP NEAR ETC XTZ EOS CHZ ENJ MANA SAND AXS GRT CRV SNX COMP BAT ZRX USDT USDC'.split())
FACTORY = '0x1f98431c8ad98523631ae4a59f267346ea31f984'
USDC = '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48'
WETH = '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2'
SWAP_TOPIC = '0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'
LARGE_TRADE = 100000.0  # USDT or USDC quote units, not an owner's identity or net worth.


def number(value, zero=False):
    value=float(value)
    if not math.isfinite(value) or value < 0 or (value == 0 and not zero):
        raise ValueError('Invalid numeric market data')
    return value


def http_json(url, body=None):
    req=urllib.request.Request(url,data=json.dumps(body).encode() if body is not None else None,
        headers={'Accept':'application/json','Content-Type':'application/json','User-Agent':'Boru-Paper/3.0'})
    with urllib.request.urlopen(req,timeout=8) as r:
        if 'json' not in r.headers.get('Content-Type',''):
            raise ValueError('Expected JSON public data')
        return json.load(r)


def candles(rows, seconds, now):
    """Validate real exchange OHLCV and discard the still-open candle."""
    if not isinstance(rows,list):
        raise ValueError('Invalid candle response')
    result=[]
    for r in rows:
        if not isinstance(r,list) or len(r)<11:
            raise ValueError('Missing candle fields')
        start,end=int(r[0])/1000,int(r[6])/1000
        if end >= now:
            continue
        o,h,l,c=[number(x) for x in r[1:5]]
        if not l<=min(o,c)<=max(o,c)<=h or abs(end-start-seconds)>.002:
            raise ValueError('Inconsistent OHLCV')
        result.append({'time':start,'end':end,'open':o,'high':h,'low':l,'close':c,
                       'volume':number(r[5],True),'quote_volume':number(r[7],True),'trades':int(r[8])})
    if any(b['time']-a['time'] != seconds for a,b in zip(result,result[1:])):
        raise ValueError('Candle history has gaps or duplicates')
    if not result or now-result[-1]['end']>seconds+120:
        raise ValueError('Candle history is stale')
    return result


def moving(values, period):
    out=values[0]
    for v in values[1:]: out+=2/(period+1)*(v-out)
    return out


def candle_patterns(bars):
    a,b=bars[-2:]
    body=abs(b['close']-b['open'])
    span=b['high']-b['low']
    lower=min(b['close'],b['open'])-b['low']
    upper=b['high']-max(b['close'],b['open'])
    return {'bullish_engulfing':a['close']<a['open'] and b['close']>b['open'] and b['open']<=a['close'] and b['close']>=a['open'],
            'bearish_engulfing':a['close']>a['open'] and b['close']<b['open'] and b['open']>=a['close'] and b['close']<=a['open'],
            'hammer':span>0 and lower>=2*max(body,span*.05) and upper<=span*.25 and b['close']>b['open'],
            'shooting_star':span>0 and upper>=2*max(body,span*.05) and lower<=span*.25,
            'doji':span>0 and body<=span*.1}


def analyze_candles(hours,days):
    if len(hours)<168 or len(days)<8:
        raise ValueError('At least seven complete days and 168 hourly candles required')
    close=[b['close'] for b in hours]
    daily=[b['close'] for b in days]
    true_ranges=[max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close'])) for a,b in zip(hours[-15:],hours[-14:])]
    average_volume=sum(b['volume'] for b in hours[-21:-1])/20
    return {'return_1h':close[-1]/hours[-1]['open']-1,
            'return_24h':close[-1]/hours[-24]['open']-1,
            'return_7d':close[-1]/hours[-168]['open']-1,
            'ema_hour_10':moving(close[-60:],10),'ema_hour_30':moving(close[-60:],30),
            'hour_trend_up':moving(close[-60:],10)>moving(close[-60:],30),
            'daily_trend_up':moving(daily,3)>moving(daily,7),
            'atr_fraction':sum(true_ranges)/len(true_ranges)/close[-1],
            'volume_ratio':hours[-1]['volume']/average_volume if average_volume>0 else None,
            'support':min(b['low'] for b in hours[-24:]),
            'resistance':max(b['high'] for b in hours[-24:]),
            'last_closed_price':close[-1],
            'patterns':candle_patterns(hours),'hourly_end':hours[-1]['end'],'daily_end':days[-1]['end']}


def large_trades(rows, now, threshold=LARGE_TRADE):
    events=[]
    seen=set()
    for r in rows:
        ts=number(r['T'])/1000
        if ts>now+5 or ts<now-300 or r['a'] in seen:
            continue
        if not isinstance(r['m'],bool):
            raise ValueError('Trade direction is not a boolean')
        seen.add(r['a'])
        value=number(r['p'])*number(r['q'])
        if value>=threshold:
            events.append({'id':r['a'],'time':ts,'notional':value,
                           'side':'SELL' if r['m'] else 'BUY','kind':'large_taker_trade',
                           'owner':'unknown','quote':'USDT'})
    first=min((number(r['T'])/1000 for r in rows),default=None)
    return {'events':events,'buy':sum(e['notional'] for e in events if e['side']=='BUY'),
            'sell':sum(e['notional'] for e in events if e['side']=='SELL'),
            'threshold_quote':threshold,'owner_verified':False,'source':'Binance aggregate trades',
            'complete_5m_window':first is not None and first<=now-300,
            'sample_from':first,'sample_to':max((number(r['T'])/1000 for r in rows),default=None)}


def signed_word(word):
    value=int(word,16)
    return value-(1<<256) if value >= 1<<255 else value


def swap_event(log,pool):
    """Decode an actual USDC/WETH Uniswap v3 Swap, not a token transfer."""
    if (log.get('removed') or log.get('address','').lower()!=pool.lower()
            or len(log.get('topics',[]))!=3 or log['topics'][0].lower()!=SWAP_TOPIC):
        return None
    payload=log.get('data','')[2:]
    if len(payload)!=64*5:
        raise ValueError('Unexpected Swap ABI payload')
    usdc,eth=signed_word(payload[:64])/1e6,signed_word(payload[64:128])/1e18
    if usdc*eth>=0:
        raise ValueError('Swap must exchange opposite asset flows')
    return {'id':log['transactionHash']+':'+log['logIndex'],'block':int(log['blockNumber'],16),
            'tx_hash':log['transactionHash'],'side':'BUY' if usdc>0 else 'SELL',
            'notional':abs(usdc),'quantity':abs(eth),'quote':'USDC','asset':'ETH',
            'kind':'confirmed_dex_swap','owner':'unknown','sender_or_router':'0x'+log['topics'][1][-40:],
            'recipient':'0x'+log['topics'][2][-40:]}


class PublicResearch:
    def __init__(self):
        self.cache={}
        self.lock=threading.Lock()
        self.pools=None

    def api(self,endpoint,**params):
        if endpoint not in ('/api/v3/exchangeInfo','/api/v3/klines','/api/v3/aggTrades'):
            raise ValueError('Only public research endpoints allowed')
        return http_json(BINANCE+endpoint+'?'+urllib.parse.urlencode(params))

    def cached(self,key,ttl,fn):
        with self.lock: value=self.cache.get(key)
        if value and time.time()-value[0]<ttl:return value[1]
        data=fn()
        with self.lock:self.cache[key]=(time.time(),data)
        return data

    def rpc(self,method,params):
        if method not in ('eth_chainId','eth_blockNumber','eth_getBlockByNumber','eth_getLogs','eth_call'):
            raise ValueError('Read-only chain methods required')
        data=http_json(RPC,{'jsonrpc':'2.0','id':1,'method':method,'params':params})
        if 'error' in data or 'result' not in data:raise ValueError('Chain RPC error: '+str(data.get('error'))[:180])
        return data['result']

    def analyze(self,market):
        base=market.removesuffix('_tl').upper()
        if not re.fullmatch('[A-Z0-9]{1,20}',base):raise ValueError('Invalid asset symbol')
        symbol=base+'USDT'
        info=self.cached('symbols',3600,lambda:self.api('/api/v3/exchangeInfo'))
        products=[s for s in info['symbols'] if s['symbol']==symbol and s['baseAsset']==base and s['quoteAsset']=='USDT' and s['status']=='TRADING']
        if len(products)!=1:return {'market':market,'status':'UNSUPPORTED_EXTERNAL_SYMBOL','received':time.time(),'identity_verified':False}
        # Hourly & daily OHLCV are in USDT on Binance, never mislabeled as Paribu TL candles.
        h=self.cached((symbol,'1h'),300,lambda:self.api('/api/v3/klines',symbol=symbol,interval='1h',limit=170))
        d=self.cached((symbol,'1d'),1800,lambda:self.api('/api/v3/klines',symbol=symbol,interval='1d',limit=32))
        trades=self.api('/api/v3/aggTrades',symbol=symbol,limit=1000)
        now=time.time()
        hours,days=candles(h,3600,now),candles(d,86400,now)
        tape=large_trades(trades,now)
        return {'market':market,'status':'READY','received':now,'venue':'Binance','quote':'USDT','symbol':symbol,
                'identity_verified':base in VERIFIED_BASES,'identity_method':'curated_base_symbol' if base in VERIFIED_BASES else 'unverified_symbol_match',
                'metrics':analyze_candles(hours,days),'hours':hours,'days':days,
                'large_trades':tape,'trade_feed_fresh':tape['sample_to'] is not None and 0<=now-tape['sample_to']<=120,'company_ownership':'NOT_VERIFIED',
                'sources':[BINANCE+'/api/v3/klines',BINANCE+'/api/v3/aggTrades']}

    def chain(self):
        if int(self.rpc('eth_chainId',[]),16)!=1:raise ValueError('Wrong Ethereum network')
        if self.pools is None:
            pools=[]
            for fee in (500,3000):
                data='0x1698ee82'+USDC[2:].zfill(64)+WETH[2:].zfill(64)+hex(fee)[2:].zfill(64)
                result=self.rpc('eth_call',[{'to':FACTORY,'data':data},'latest'])
                pool='0x'+result[-40:]
                if int(pool,16)==0:continue
                token0=self.rpc('eth_call',[{'to':pool,'data':'0x0dfe1681'},'latest'])
                token1=self.rpc('eth_call',[{'to':pool,'data':'0xd21220a7'},'latest'])
                factory=self.rpc('eth_call',[{'to':pool,'data':'0xc45a0155'},'latest'])
                if '0x'+token0[-40:].lower()!=USDC or '0x'+token1[-40:].lower()!=WETH or '0x'+factory[-40:].lower()!=FACTORY:
                    raise ValueError('Unverified pool identity')
                pools.append(pool.lower())
            self.pools=pools
        if not self.pools:raise ValueError('No verified ETH/USDC pools')
        head=int(self.rpc('eth_blockNumber',[]),16)
        end=head-12
        block=self.rpc('eth_getBlockByNumber',[hex(end),False])
        stamp=int(block['timestamp'],16)
        now=time.time()
        if now-stamp>900 or stamp>now+5:raise ValueError('Stale confirmed chain block')
        logs=self.rpc('eth_getLogs',[{'fromBlock':hex(max(0,end-25)),'toBlock':hex(end),'address':self.pools,'topics':[SWAP_TOPIC]}])
        seen,events=set(),[]
        for log in logs:
            event=swap_event(log,log['address']) if log.get('address','').lower() in self.pools else None
            if event and event['id'] not in seen:
                seen.add(event['id'])
                if event['notional']>=LARGE_TRADE:events.append(event)
        return {'status':'READY','received':now,'chain':'ethereum','asset':'ETH','pools':self.pools,
                'confirmed_through':end,'block_hash':block['hash'],'block_time':stamp,'confirmations':12,
                'buy':sum(e['notional'] for e in events if e['side']=='BUY'),
                'sell':sum(e['notional'] for e in events if e['side']=='SELL'),
                'events':events,'source':RPC,'scope':'two verified ETH/USDC Uniswap v3 pools; not all wallets or chains',
                'company_ownership':'NOT_VERIFIED'}


class ResearchEngine:
    """Bounded background I/O cannot hold up position exits. SQLite writes stay in the caller."""
    def __init__(self,provider=None):
        from chains import ChainMonitor
        self.provider=provider or PublicResearch()
        self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self.pending={};self.data={};self.attempted={};self.chain_data={}
        self.chain_monitor=ChainMonitor()

    def step(self,markets,held,now):
        updates=self.chain_monitor.step(now)
        for market,future in list(self.pending.items()):
            if not future.done():continue
            del self.pending[market]
            try: result=future.result()
            except Exception as exc:result={'status':'DATA_ERROR','received':now,'error':type(exc).__name__+': '+str(exc)[:180]}
            if market=='__chain__':self.chain_data=result
            else:self.data[market]=result
            updates.append((market,result))
        due=sorted(markets,key=lambda m:(0 if m in held else 1,self.attempted.get(m,0),m))
        if now-self.attempted.get('__chain__',0)>=60:due.insert(0,'__chain__')
        for market in due:
            if len(self.pending)>=4:break
            ttl=60 if market in held or market=='__chain__' else 300
            if market in self.pending or now-self.attempted.get(market,0)<ttl:continue
            self.attempted[market]=now
            self.pending[market]=self.pool.submit(self.provider.chain if market=='__chain__' else self.provider.analyze,*(() if market=='__chain__' else (market,)))
        return updates

    def context(self,market,now):
        result=dict(self.data.get(market,{'status':'PENDING','received':0}))
        result['fresh']=result.get('status')=='READY' and 0<=now-result.get('received',0)<=360
        chain=self.chain_data if market=='eth_tl' else {'status':'UNSUPPORTED_ASSET','scope':'No verified on-chain adapter for this asset'}
        result['chain']=dict(chain)
        result['chain']['fresh']=chain.get('status')=='READY' and 0<=now-chain.get('received',0)<=180
        # Native transfer observations never stand in for directional DEX evidence.
        result['chain_activity']=self.chain_monitor.context(market.removesuffix('_tl').upper(),now)
        return result

    def close(self):
        self.pool.shutdown(wait=True,cancel_futures=True)
        self.chain_monitor.close()
