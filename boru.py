"""BÖRÜ forward paper trading: public market GETs only; no real orders."""
import argparse
import concurrent.futures
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import statistics
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

CAPITAL, STOP, TARGET, FEE = 20000.0, -0.02, 0.03, 0.0035
WARMUP, PARTICIPATION = 60, 0.10


def utc(ts=None):
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).isoformat()


def positive(value):
    x = float(value)
    if not math.isfinite(x) or x <= 0:
        raise ValueError("Invalid market value")
    return x


def validate_book(data, now=None):
    now = time.time() if now is None else now
    if not isinstance(data, dict):
        raise ValueError("Missing order book")
    stamp = positive(data["timestamp"])
    if stamp > 1e12:
        stamp /= 1000
    if not -5 <= now-stamp <= 30:
        raise ValueError("Stale order book")
    result = {"timestamp": stamp}
    for side in ("bids", "asks"):
        levels = [(positive(p), positive(q)) for p, q in data[side]]
        if not levels:
            raise ValueError("Empty order book")
        result[side] = sorted(levels, reverse=(side == "bids"))
    if result["bids"][0][0] >= result["asks"][0][0]:
        raise ValueError("Crossed order book")
    return result


class MarketData:
    def get(self, endpoint, **params):
        if endpoint not in ("/market/ticker", "/orderbook"):
            raise ValueError("Only public market data is supported")
        url = "https://api.paribu.com" + endpoint
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Boru-Paper/1.0"}, method="GET")
        with urllib.request.urlopen(req, timeout=12) as response:
            if "json" not in response.headers.get("Content-Type", ""):
                raise ValueError("Market feed did not return JSON")
            return json.load(response)

    def tickers(self):
        data = self.get("/market/ticker")
        if not isinstance(data, list):
            raise ValueError("Invalid ticker response")
        result = {}
        for item in data:
            try:
                market, volume = item["market"], positive(item["pair_volume"])
                positive(item["last"])
                if market.endswith("_tl"):
                    result[market] = volume
            except (KeyError, TypeError, ValueError):
                continue
        if not result:
            raise ValueError("No valid TL markets")
        return result

    def book(self, market):
        return validate_book(self.get("/orderbook", market=market, limit=100))


def sweep(levels, *, quantity=None, budget=None):
    """Use at most 10% of visible quantity; do not invent liquidity."""
    if (quantity is None) == (budget is None):
        raise ValueError("Specify quantity or budget")
    filled, notional = 0.0, 0.0
    for price, available in levels:
        remaining = quantity-filled if quantity is not None else (budget-notional)/price
        take = max(0.0, min(available*PARTICIPATION, remaining))
        filled, notional = filled+take, notional+take*price
        if (quantity is not None and filled >= quantity-1e-12) or (budget is not None and notional >= budget-1e-8):
            break
    return filled, notional


def ema(values, period):
    out, alpha = values[0], 2/(period+1)
    for value in values[1:]:
        out += alpha*(value-out)
    return out


def valid_history(bars):
    recent = bars[-WARMUP:]
    return (len(recent) == WARMUP and all(b["samples"] >= 2 for b in recent)
            and all(b["minute"]-a["minute"] == 60 for a, b in zip(recent, recent[1:])))


def signal_for(bars):
    # Closed sampled quote bars, not transaction OHLCV.
    if not valid_history(bars):
        return None
    recent = bars[-WARMUP:]
    closes = [b["close"] for b in recent]
    changes = [b-a for a, b in zip(closes[-15:], closes[-14:])]
    gain, loss = sum(max(x,0) for x in changes)/14, sum(max(-x,0) for x in changes)/14
    rsi = (100 if gain else 50) if loss == 0 else 100-100/(1+gain/loss)
    fast, slow = ema(closes,10), ema(closes,30)
    volatility = statistics.pstdev(math.log(b/a) for a,b in zip(closes[-21:],closes[-20:]))
    momentum = closes[-1]/closes[-6]-1
    breakout = closes[-1] > max(b["high"] for b in recent[-21:-1])
    if fast > slow and breakout and 50 <= rsi <= 75 and 0.0001 <= volatility <= 0.008 and momentum > 0:
        return {"score":momentum/volatility,"rsi":rsi,"ema10":fast,"ema30":slow,"volatility_1m":volatility}
    return None


class Simulation:
    def __init__(self, directory, feed=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory/"simulation.sqlite3")
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY CHECK(id=1),cash REAL,realized REAL);
            CREATE TABLE IF NOT EXISTS positions (market TEXT PRIMARY KEY,qty REAL,cost REAL,opened REAL);
            CREATE TABLE IF NOT EXISTS bars (market TEXT,minute INTEGER,high REAL,low REAL,close REAL,samples INTEGER,PRIMARY KEY(market,minute));
            CREATE TABLE IF NOT EXISTS ticks (market TEXT,timestamp REAL,bid REAL,ask REAL,PRIMARY KEY(market,timestamp));
            CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY,ts REAL,market TEXT,side TEXT,qty REAL,price REAL,fee REAL,pnl REAL,reason TEXT);
            CREATE TABLE IF NOT EXISTS cycles (ts REAL,valid_books INTEGER,errors TEXT);
        """)
        self.db.execute("INSERT OR IGNORE INTO account VALUES(1,?,0)",(CAPITAL,))
        self.db.execute("INSERT OR IGNORE INTO settings VALUES('started',?)",(utc(),))
        self.db.commit()
        self.feed = feed or MarketData()
        self._reported_count = 0

    def cash(self):
        return self.db.execute("SELECT cash FROM account WHERE id=1").fetchone()[0]

    def positions(self):
        return self.db.execute("SELECT market,qty,cost,opened FROM positions").fetchall()

    def record_book(self, market, book, now):
        bid, ask = book["bids"][0][0],book["asks"][0][0]
        cur = self.db.execute("INSERT OR IGNORE INTO ticks VALUES(?,?,?,?)",(market,book["timestamp"],bid,ask))
        if not cur.rowcount:
            return
        minute, mid = int(book["timestamp"]//60)*60,(bid+ask)/2
        self.db.execute("""INSERT INTO bars VALUES(?,?,?,?,?,1) ON CONFLICT(market,minute) DO UPDATE SET
            high=MAX(high,excluded.high),low=MIN(low,excluded.low),close=excluded.close,samples=samples+1""",
            (market,minute,mid,mid,mid))

    def buy(self, market, book, budget, now, sig):
        if budget < 1000:
            return False
        qty,notional = sweep(book["asks"],budget=min(budget,self.cash())/(1+FEE))
        if notional < 1000 or qty <= 0 or notional/qty/book["asks"][0][0]-1 > 0.003:
            return False
        cost,fee = notional*(1+FEE),notional*FEE
        self.db.execute("UPDATE account SET cash=cash-? WHERE id=1",(cost,))
        self.db.execute("INSERT INTO positions VALUES(?,?,?,?)",(market,qty,cost,now))
        self.db.execute("INSERT INTO trades VALUES(NULL,?,?,?,?,?,?,?,?)",(now,market,"BUY",qty,notional/qty,fee,0,json.dumps(sig)))
        return True

    def sell(self, position, book, now):
        market,qty,cost,opened = position
        filled,gross = sweep(book["bids"],quantity=qty)
        if filled <= 1e-12:
            return
        allocated,net = cost*filled/qty,gross*(1-FEE)
        change = net/allocated-1
        if STOP < change < TARGET:
            return
        pnl = net-allocated
        self.db.execute("UPDATE account SET cash=cash+?,realized=realized+? WHERE id=1",(net,pnl))
        if filled >= qty-1e-12:
            self.db.execute("DELETE FROM positions WHERE market=?",(market,))
        else:
            self.db.execute("UPDATE positions SET qty=qty-?,cost=cost-? WHERE market=?",(filled,allocated,market))
        self.db.execute("INSERT INTO trades VALUES(NULL,?,?,?,?,?,?,?,?)",(now,market,"SELL",filled,gross/filled,gross*FEE,pnl,"STOP" if change <= STOP else "TARGET"))

    def closed_bars(self, market, now):
        rows = self.db.execute("SELECT minute,high,low,close,samples FROM bars WHERE market=? AND minute<? ORDER BY minute DESC LIMIT 60",(market,int(now//60)*60)).fetchall()
        return [dict(zip(("minute","high","low","close","samples"),row)) for row in reversed(rows)]

    def cycle(self):
        errors,tickers = [],{}
        try:
            tickers = self.feed.tickers()
        except Exception as exc:
            errors.append("ticker: "+type(exc).__name__+": "+str(exc)[:150])
        eligible = sorted((m for m in tickers if tickers[m] >= 5000000 and m not in ("usdt_tl","usdc_tl")),key=lambda m:tickers[m],reverse=True)[:10]
        markets = set(eligible)|{p[0] for p in self.positions()}
        books = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(self.feed.book,m):m for m in markets}
            for future in concurrent.futures.as_completed(futures):
                market = futures[future]
                try:
                    books[market] = future.result()
                except Exception as exc:
                    errors.append(market+": "+type(exc).__name__+": "+str(exc)[:120])
        now = time.time()
        for market in list(books):
            if not -5 <= now-books[market]["timestamp"] <= 30:
                del books[market]
                errors.append(market+": expired during fetch")
        with self.db:
            for market,book in books.items():
                self.record_book(market,book,now)
            for position in self.positions():
                if position[0] in books:
                    self.sell(position,books[position[0]],now)
            held = {p[0] for p in self.positions()}
            candidates = []
            for market in eligible:
                if market in held or market not in books:
                    continue
                last_exit = self.db.execute("SELECT MAX(ts) FROM trades WHERE market=? AND side='SELL'",(market,)).fetchone()[0]
                if last_exit is not None and now-last_exit < 1800:
                    continue
                book = books[market]
                if book["asks"][0][0]/book["bids"][0][0]-1 > 0.003:
                    continue
                bars = self.closed_bars(market,now)
                if not bars or bars[-1]["minute"] != int(now//60)*60-60:
                    continue
                sig = signal_for(bars)
                if sig:
                    candidates.append((market,sig))
            chosen = sorted(candidates,key=lambda x:x[1]["score"],reverse=True)[:max(0,3-len(held))]
            if chosen and all(m in books for m in held):
                budget,weight = self.cash(),sum(s["score"] for _,s in chosen)
                for market,sig in chosen:
                    self.buy(market,books[market],budget*sig["score"]/weight,now,sig)
            self.db.execute("INSERT INTO cycles VALUES(?,?,?)",(now,len(books),json.dumps(errors)))
            self.db.execute("DELETE FROM ticks WHERE timestamp<?",(now-7*86400,))
            self.db.execute("DELETE FROM bars WHERE minute<?",(now-30*86400,))
        report = self.report(now,books,errors,eligible)
        tmp = self.directory/"status.tmp"
        tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        tmp.replace(self.directory/"status.json")
        with (self.directory/"reports.jsonl").open("a",encoding="utf-8") as handle:
            handle.write(json.dumps(report,ensure_ascii=False)+"\n")
        print(json.dumps(report,ensure_ascii=False),flush=True)
        if os.environ.get('BORU_GRAPH_REPORTS') == '1' and report['trade_events'] > self._reported_count:
            try:
                from reporting import generate
                index = generate(self.directory)
                self._reported_count = len(index['trades'])
                print('BORU_REPORTS_READY ' + json.dumps({'session_id':index['session_id'],'count':self._reported_count}),flush=True)
            except Exception as exc:
                # Report failure must not stop position management; retry next cycle.
                print('REPORT_ERROR ' + type(exc).__name__ + ': ' + str(exc)[:150],flush=True)
        return report

    def report(self, now, books, errors, eligible):
        positions,value,complete = [],0.0,True
        for market,qty,cost,opened in self.positions():
            book,net = books.get(market),None
            if book:
                filled,gross = sweep(book["bids"],quantity=qty)
                if filled >= qty-1e-12:
                    net = gross*(1-FEE)
            if net is None:
                complete = False
            else:
                value += net
            positions.append({"market":market,"quantity":qty,"cost_tl":round(cost,2),"estimated_net_sale_tl":round(net,2) if net is not None else None})
        cash = self.cash()
        equity = cash+value if complete else None
        counts = {m:len(self.closed_bars(m,now)) for m in eligible}
        ready = any(valid_history(self.closed_bars(m,now)) for m in eligible)
        return {"mode":"PAPER_ONLY","timestamp_utc":utc(now),
            "started_utc":self.db.execute("SELECT value FROM settings WHERE key='started'").fetchone()[0],
            "status":"DATA_ERROR" if not books else "WARMING_UP" if not ready else "MONITORING",
            "starting_capital_tl":CAPITAL,"cash_tl":round(cash,2),
            "estimated_equity_tl":round(equity,2) if equity is not None else None,
            "estimated_total_pnl_tl":round(equity-CAPITAL,2) if equity is not None else None,
            "realized_pnl_tl":round(self.db.execute("SELECT realized FROM account WHERE id=1").fetchone()[0],2),
            "trade_events":self.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0],
            "positions":positions,"valid_books":len(books),"closed_quote_minutes":counts,
            "required_consecutive_minutes":WARMUP,"assumed_fee_each_side":FEE,
            "stop_net_return":STOP,"target_net_return":TARGET,"errors":errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory",default=os.environ.get("BORU_DATA_DIR","data"))
    parser.add_argument("--duration-minutes",type=float,default=180)
    parser.add_argument("--interval",type=float,default=15)
    parser.add_argument("--cycles",type=int,default=0)
    parser.add_argument("--require-data",action="store_true")
    args = parser.parse_args()
    if args.interval < 10 or args.duration_minutes < 0 or args.cycles < 0:
        parser.error("interval >=10; duration/cycles >=0 required")
    sim,stopping = Simulation(args.directory),False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    start,count,valid = time.monotonic(),0,0
    try:
        while not stopping:
            tick = time.monotonic()
            report = sim.cycle()
            count += 1
            valid += int(report["valid_books"] > 0)
            if args.cycles and count >= args.cycles:
                break
            if args.duration_minutes and time.monotonic()-start >= args.duration_minutes*60:
                break
            delay = max(0,args.interval-(time.monotonic()-tick))
            if not report["valid_books"] or any("429" in e for e in report["errors"]):
                delay = max(delay,60)
            deadline = time.monotonic()+delay
            while not stopping and time.monotonic() < deadline:
                time.sleep(min(1,max(0,deadline-time.monotonic())))
    finally:
        sim.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        sim.db.close()
    if args.require_data and valid == 0:
        raise SystemExit("No valid live market data; simulation did not start")


if __name__ == "__main__":
    main()
