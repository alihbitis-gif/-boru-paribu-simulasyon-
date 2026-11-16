"""Read-only discovery of public market-data contracts; never opens a paper account."""
import concurrent.futures, json, urllib.request
URLS=[
 "https://www.paribu.com/chart/history?symbol=btc_tl&period=60&type=basic",
 "https://www.paribu.com/chart/history?symbol=btc_tl&period=1h&type=basic",
 "https://www.paribu.com/market/btc_tl/latest-matches",
 "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=170",
 "https://data-api.binance.vision/api/v3/aggTrades?symbol=BTCUSDT&limit=3",
 "https://ethereum-rpc.publicnode.com",
]
def probe(url):
 try:
  body=None
  if "publicnode" in url:
   body=json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}).encode()
  req=urllib.request.Request(url,data=body,headers={"User-Agent":"Boru-Paper/3.0","Accept":"application/json","Content-Type":"application/json"})
  with urllib.request.urlopen(req,timeout=12) as r:
   text=r.read(1000000).decode()
   data=json.loads(text)
   sample=data[:2] if isinstance(data,list) else data
   return {"url":url,"status":r.status,"count":len(data),"sample":str(sample)[:2000]}
 except Exception as e:
  return {"url":url,"error":type(e).__name__+": "+str(e)[:200]}
if __name__=="__main__":
 with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
  for row in pool.map(probe,URLS):print(json.dumps(row,ensure_ascii=False),flush=True)
