"""BÖRÜ forward paper trading: public market GETs only; no real orders."""
import argparse
import fcntl
from dataclasses import asdict, dataclass
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

CAPITAL, STOP, TARGET, FEE = 20000.0, -0.02, 0.03, 0.0028
WARMUP, PARTICIPATION = 60, 0.10


@dataclass(frozen=True)
class Config:
    version: str = "3.0"
    hard_stop: float = -.05
    fee: float = FEE
    fee_source: str = "Paribu standard TL taker tier 1; checked 2026-11-16; user tier unverified"
    risk_per_position: float = .01
    portfolio_risk: float = .02
    gap_reserve: float = .005
    max_roundtrip_cost: float = .008
    breakout_buffer: float = .001
    max_positions: int = 3

    def __post_init__(self):
        for value in (self.fee, self.risk_per_position, self.portfolio_risk,
                      self.gap_reserve, self.max_roundtrip_cost, self.breakout_buffer):
            if not math.isfinite(value) or not 0 <= value < 1:
                raise ValueError("Invalid configuration fraction")
        if not -.20 <= self.hard_stop <= -.02:
            raise ValueError("Hard stop must be between -20% and -2%")
        if not 0 < self.risk_per_position <= self.portfolio_risk or self.max_positions < 1:
            raise ValueError("Invalid risk limits")


def validate_tickers(data):
    if not isinstance(data, list):
        raise ValueError("Invalid ticker response")
    result = {}
    for item in data:
        try:
            market = item["market"]
            if market.endswith("_tl"):
                result[market] = {"volume_24h_tl": nonnegative(item["pair_volume"]),
                                  "last": positive(item["last"])}
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    if not result:
        raise ValueError("No valid TL markets")
    return result


def utc(ts=None):
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).isoformat()


def nonnegative(value):
    x=float(value)
    if not math.isfinite(x) or x<0:raise ValueError("Invalid nonnegative market value")
    return x


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
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Boru-Paper/2.0"}, method="GET")
        with urllib.request.urlopen(req, timeout=12) as response:
            if "json" not in response.headers.get("Content-Type", ""):
                raise ValueError("Market feed did not return JSON")
            return json.load(response)

    def tickers(self):
        return validate_tickers(self.get("/market/ticker"))

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
    # Closed sampled bars; the caller selects bid (v2) or midpoint (v1 diagnostics).
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


def trend_up(bars):
    if not valid_history(bars):
        return False
    groups = {}
    for b in bars:
        groups.setdefault(b["minute"]//300,[]).append(b)
    closes = [g[-1]["close"] for g in groups.values() if len(g)==5]
    return len(closes)>=8 and ema(closes,3)>ema(closes,8) and closes[-1]>=closes[-2]


class Simulation:
    def __init__(self, directory, feed=None, config=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        existing = self.directory/"simulation.sqlite3"
        if existing.exists():
            probe = sqlite3.connect(existing.resolve().as_uri()+"?mode=ro", uri=True)
            try:
                settings = probe.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
                if settings and not probe.execute("SELECT 1 FROM settings WHERE key='config'").fetchone():
                    raise ValueError("Legacy session is read-only; start v2 in a separate directory")
            finally:
                probe.close()
        self.lock = (self.directory/"simulation.lock").open("a")
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError("Another simulation owns this data directory")
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
            CREATE TABLE IF NOT EXISTS books (market TEXT,timestamp REAL,received REAL,payload TEXT,PRIMARY KEY(market,timestamp));
            CREATE TABLE IF NOT EXISTS ticker_observations (market TEXT,received REAL,last REAL,volume_24h_tl REAL,PRIMARY KEY(market,received));
            CREATE TABLE IF NOT EXISTS decisions (ts REAL,market TEXT,action TEXT,reason TEXT,details TEXT);
            CREATE TABLE IF NOT EXISTS fills (trade_id INTEGER PRIMARY KEY,fee_rate REAL,fee_currency TEXT,fee_quantity REAL,gross_qty REAL,notional REAL,book_ts REAL);
            CREATE TABLE IF NOT EXISTS used_books (market TEXT,timestamp REAL,PRIMARY KEY(market,timestamp));
            CREATE TABLE IF NOT EXISTS exit_intents (market TEXT PRIMARY KEY,reason TEXT);
            CREATE TABLE IF NOT EXISTS shadow_signals (ts REAL,market TEXT,v1 TEXT,v2 TEXT);
            CREATE TABLE IF NOT EXISTS research_snapshots (ts REAL,market TEXT,payload TEXT);
            CREATE TABLE IF NOT EXISTS candles (market TEXT,venue TEXT,interval TEXT,time REAL,payload TEXT,PRIMARY KEY(market,venue,interval,time));
            CREATE TABLE IF NOT EXISTS execution_context (trade_id INTEGER PRIMARY KEY,version TEXT,payload TEXT);
            CREATE TABLE IF NOT EXISTS position_metrics (market TEXT PRIMARY KEY,peak_return REAL);


        """)
        saved = self.db.execute("SELECT value FROM settings WHERE key='config'").fetchone()
        if saved:
            self.config = Config(**json.loads(saved[0]))
            if config is not None and config != self.config:
                self.close()
                raise ValueError("Existing session configuration is immutable; start a separate directory")
        else:
            if self.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]:
                self.close()
                raise ValueError("Legacy session: keep it read-only; start v2 in a separate directory")
            self.config = config or Config()
            self.db.execute("INSERT INTO settings VALUES('config',?)", (json.dumps(asdict(self.config)),))
        self.db.execute("INSERT OR IGNORE INTO account VALUES(1,?,0)",(CAPITAL,))
        self.db.execute("INSERT OR IGNORE INTO settings VALUES('started',?)",(utc(),))
        self.db.commit()
        self.feed = feed or MarketData()
        self._reported_count = 0
        self.entries_open = True
        self.research = None
        self.universe_count = 0
        self._book_rotation = 0
        if self.config.version == '3.0':
            from market_intel import ResearchEngine
            self.research = ResearchEngine()

    def close(self):
        if getattr(self,'research',None):self.research.close()
        self.db.close()
        self.lock.close()

    def decision(self, now, market, action, reason, **details):
        self.db.execute("INSERT INTO decisions VALUES(?,?,?,?,?)", (now, market, action, reason, json.dumps(details)))

    def usable(self, market, book, now):
        return (-5 <= now-book["timestamp"] <= 30 and not self.db.execute(
            "SELECT 1 FROM used_books WHERE market=? AND timestamp=?", (market, book["timestamp"])).fetchone())


    def cash(self):
        return self.db.execute("SELECT cash FROM account WHERE id=1").fetchone()[0]

    def positions(self):
        return self.db.execute("SELECT market,qty,cost,opened FROM positions").fetchall()

    def record_book(self, market, book, now):
        self.db.execute("INSERT OR IGNORE INTO books VALUES(?,?,?,?)", (market, book["timestamp"], now, json.dumps(book)))
        bid, ask = book["bids"][0][0],book["asks"][0][0]
        cur = self.db.execute("INSERT OR IGNORE INTO ticks VALUES(?,?,?,?)",(market,book["timestamp"],bid,ask))
        if not cur.rowcount:
            return
        minute, mid = int(book["timestamp"]//60)*60,bid
        self.db.execute("""INSERT INTO bars VALUES(?,?,?,?,?,1) ON CONFLICT(market,minute) DO UPDATE SET
            high=MAX(high,excluded.high),low=MIN(low,excluded.low),close=excluded.close,samples=samples+1""",
            (market,minute,mid,mid,mid))

    def buy(self, market, book, budget, now, sig):
        if not self.usable(market, book, now):
            self.decision(now, market, "SKIP", "STALE_OR_USED_BOOK")
            return False
        if budget < 1000:
            self.decision(now, market, "SKIP", "RISK_BUDGET_TOO_SMALL", budget=budget)
            return False
        gross_qty, notional = sweep(book["asks"], budget=min(budget, self.cash()))
        if notional < 1000 or gross_qty <= 0:
            self.decision(now, market, "SKIP", "INSUFFICIENT_BUY_DEPTH")
            return False
        impact = notional/gross_qty/book["asks"][0][0]-1
        qty = gross_qty*(1-self.config.fee)
        liquidation_qty, liquidation_gross = sweep(book["bids"], quantity=qty)
        drag = 1-liquidation_gross*(1-self.config.fee)/notional
        if (impact > .003 or liquidation_qty < qty-1e-10 or drag > self.config.max_roundtrip_cost):
            self.decision(now, market, "SKIP", "EXECUTION_COST_OR_EXIT_DEPTH", impact=impact, roundtrip_cost=drag)
            return False
        fee = notional*self.config.fee  # TL equivalent; deducted in the acquired asset, not twice in cash.
        self.db.execute("UPDATE account SET cash=cash-? WHERE id=1", (notional,))
        self.db.execute("INSERT INTO positions VALUES(?,?,?,?)", (market,qty,notional,now))
        cur = self.db.execute("INSERT INTO trades VALUES(NULL,?,?,?,?,?,?,?,?)", (now,market,"BUY",qty,notional/gross_qty,fee,0,json.dumps(sig)))
        self.db.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?)", (cur.lastrowid,self.config.fee,market.split('_')[0].upper(),gross_qty*self.config.fee,gross_qty,notional,book["timestamp"]))
        self.db.execute("INSERT INTO used_books VALUES(?,?)", (market,book["timestamp"]))
        self.db.execute("INSERT OR REPLACE INTO position_metrics VALUES(?,-1)",(market,))
        self.db.execute("INSERT INTO execution_context VALUES(?,?,?)", (cur.lastrowid,self.config.version,json.dumps(dict(sig,config=asdict(self.config)))))
        self.decision(now,market,"BUY","CONFIRMED_SIGNAL",budget=budget,cost=notional,roundtrip_cost=drag,signal=sig)
        return True

    def sell(self, position, book, now):
        market,qty,cost,opened = position
        if not self.usable(market,book,now):
            self.decision(now,market,"HOLD","STALE_OR_USED_BOOK")
            return
        filled,gross = sweep(book["bids"],quantity=qty)
        if filled <= 1e-12:
            self.decision(now,market,"HOLD","NO_EXIT_DEPTH")
            return
        allocated,net = cost*filled/qty,gross*(1-self.config.fee)
        change = net/allocated-1
        intent = self.db.execute("SELECT reason FROM exit_intents WHERE market=?", (market,)).fetchone()
        context={}
        if self.config.version=='3.0':
            from strategy import exit_decision,compact
            context=compact(self.research.context(market,now))
            peak_row=self.db.execute("SELECT peak_return FROM position_metrics WHERE market=?",(market,)).fetchone()
            peak=max(peak_row[0] if peak_row else -1,change)
            self.db.execute("INSERT OR REPLACE INTO position_metrics VALUES(?,?)",(market,peak))
            should_exit,reason=exit_decision(change,peak,context,self.config.hard_stop)
            if intent:should_exit,reason=True,intent[0]
            self.decision(now,market,'EXIT_REVIEW',reason,net_return=change,peak_return=peak,research=context)
            if not should_exit:return
            self.db.execute("INSERT OR IGNORE INTO exit_intents VALUES(?,?)",(market,reason))
        else:
            if not intent and STOP < change < TARGET:
                self.decision(now,market,"HOLD","BETWEEN_THRESHOLDS",net_return=change,fillable_qty=filled)
                return
            reason = "STOP" if intent or change <= STOP else "TARGET"
            if reason == "STOP":
                self.db.execute("INSERT OR IGNORE INTO exit_intents VALUES(?,'STOP')", (market,))
        pnl = net-allocated
        self.db.execute("UPDATE account SET cash=cash+?,realized=realized+? WHERE id=1",(net,pnl))
        if filled >= qty-1e-12:
            self.db.execute("DELETE FROM positions WHERE market=?",(market,))
            self.db.execute("DELETE FROM exit_intents WHERE market=?",(market,))
            self.db.execute("DELETE FROM position_metrics WHERE market=?",(market,))
        else:
            self.db.execute("UPDATE positions SET qty=qty-?,cost=cost-? WHERE market=?",(filled,allocated,market))
        cur = self.db.execute("INSERT INTO trades VALUES(NULL,?,?,?,?,?,?,?,?)", (now,market,"SELL",filled,gross/filled,gross*self.config.fee,pnl,reason))
        self.db.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?)", (cur.lastrowid,self.config.fee,"TL",gross*self.config.fee,filled,gross,book["timestamp"]))
        self.db.execute("INSERT INTO used_books VALUES(?,?)", (market,book["timestamp"]))
        self.db.execute("INSERT INTO execution_context VALUES(?,?,?)",(cur.lastrowid,self.config.version,json.dumps({'decision':reason,'research':context,'config':asdict(self.config)})))
        self.decision(now,market,"SELL",reason,net_return=change,pnl=pnl,partial=filled<qty-1e-12)

    def closed_bars(self, market, now):
        rows = self.db.execute("SELECT minute,high,low,close,samples FROM bars WHERE market=? AND minute<? ORDER BY minute DESC LIMIT 60",(market,int(now//60)*60)).fetchall()
        return [dict(zip(("minute","high","low","close","samples"),row)) for row in reversed(rows)]

    def confirmed_signal(self, market, book, now):
        if self.config.version=='3.0':
            from strategy import entry_decision,compact
            context=self.research.context(market,now)
            recent=self.db.execute("SELECT last FROM ticker_observations WHERE market=? AND received>=? ORDER BY received",(market,now-120)).fetchall()
            rebound=len(recent)>=2 and recent[-1][0]>min(r[0] for r in recent[:-1]) and book['bids'][0][0]>=recent[-1][0]*.997
            accepted,reason=entry_decision(context,rebound)
            if not accepted:return None,reason
            # Reject grossly inconsistent cross-venue asset/quote mapping.
            usdt=self.db.execute("SELECT last FROM ticker_observations WHERE market='usdt_tl' AND received>=? ORDER BY received DESC LIMIT 1",(now-30,)).fetchone()
            price=context['metrics']['last_closed_price']
            if not usdt or not .8 <= recent[-1][0]/(price*usdt[0]) <= 1.2:
                return None,'CROSS_VENUE_PRICE_UNCONFIRMED'
            metrics=context['metrics']
            return {'score':max(.001,metrics['return_1h']+metrics['return_24h'])/max(.001,metrics['atr_fraction']),
                    'decision':reason,'research':compact(context),'local_rebound':rebound},reason
        bars = self.closed_bars(market, now)
        if not bars or bars[-1]["minute"] != int(now//60)*60-60 or not valid_history(bars):
            return None, "WARMUP_OR_GAP"
        sig = signal_for(bars)
        if not sig:
            return None, "TECHNICAL_FILTER"
        prior_high = max(b["high"] for b in bars[-21:-1])
        buffer = max(self.config.breakout_buffer, sig["volatility_1m"]*.5)
        level = prior_high*(1+buffer)
        if bars[-1]["close"] < level or book["bids"][0][0] < level:
            return None, "BID_BREAKOUT_TOO_SMALL_OR_LOST"
        if not trend_up(bars):
            return None, "FIVE_MINUTE_TREND"
        if market != "btc_tl":
            btc = self.closed_bars("btc_tl",now)
            stamp = self.db.execute("SELECT MAX(timestamp) FROM books WHERE market='btc_tl'").fetchone()[0]
            if (stamp is None or not -5 <= now-stamp <= 30 or not btc
                    or btc[-1]["minute"] != int(now//60)*60-60 or not trend_up(btc)):
                return None, "BTC_TREND_UNCONFIRMED"
        # A ticker sample is not a trade tape or a one-minute volume bar.
        samples = self.db.execute("SELECT received,last FROM ticker_observations WHERE market=? AND received>=? ORDER BY received", (market,bars[-1]["minute"])).fetchall()
        if (len(samples) < 2 or now-samples[-1][0] > 30 or samples[-1][1] < level
                or not any(a[1] != b[1] for a,b in zip(samples,samples[1:]))):
            return None, "LAST_TRADE_PRICE_UNCONFIRMED"
        sig.update(prior_high=prior_high,breakout_level=level,breakout_buffer=buffer,
                   last_observed_trade_price=samples[-1][1],source="bid_quotes_and_sampled_last_trade",
                   confirmation="bid breakout + changed last-price observation + five-minute trend")
        return sig, "CONFIRMED"

    def legacy_signal(self, market, now):
        # Diagnostic comparison only: no second account or invented P&L.
        rows = self.db.execute("""SELECT CAST(timestamp/60 AS INTEGER)*60 AS minute,
            MAX((bid+ask)/2),MIN((bid+ask)/2),
            COUNT(*) FROM ticks WHERE market=? AND timestamp>=? AND timestamp<?
            GROUP BY minute ORDER BY minute""", (market,int(now//60)*60-3600,int(now//60)*60)).fetchall()
        bars = []
        for minute,high,low,count in rows:
            close = self.db.execute("SELECT (bid+ask)/2 FROM ticks WHERE market=? AND timestamp>=? AND timestamp<? ORDER BY timestamp DESC LIMIT 1",(market,minute,minute+60)).fetchone()[0]
            bars.append(dict(minute=minute,high=high,low=low,close=close,samples=count))
        return signal_for(bars)

    def allocate(self, candidates, books, now):
        positions = self.positions()
        valuation = self.report(now,books,[],[])["estimated_equity_tl"]
        if valuation is None:
            for market,_ in candidates:
                self.decision(now,market,"SKIP","UNKNOWN_PORTFOLIO_VALUE")
            return
        # Risk is a sizing assumption, not a guarantee that a stop will fill.
        risk_base = min(CAPITAL,valuation)
        stress = abs(self.config.hard_stop if self.config.version=="3.0" else STOP)+self.config.gap_reserve
        used_risk = sum(p[2]*stress for p in positions)
        remaining = max(0,risk_base*self.config.portfolio_risk-used_risk)
        chosen = sorted(candidates,key=lambda x:(-x[1]["score"],x[0]))
        slots = self.config.max_positions-len(positions)
        for market,sig in chosen:
            if slots <= 0:
                self.decision(now,market,"SKIP","POSITION_LIMIT")
                continue
            budget = min(self.cash(),risk_base*self.config.risk_per_position/stress,remaining/stress)
            sig.update(risk_budget_tl=min(risk_base*self.config.risk_per_position,remaining),
                       stop_and_gap_fraction=stress,proposed_budget_tl=budget)
            before = self.cash()
            if self.buy(market,books[market],budget,now,sig):
                remaining -= (before-self.cash())*stress
                slots -= 1

    def cycle(self, allow_entries=True, entry_deadline=None):
        self.entries_open = allow_entries
        errors,tickers = [],{}
        try:
            tickers = self.feed.tickers()
            ticker_received = time.time()
        except Exception as exc:
            errors.append("ticker: "+type(exc).__name__+": "+str(exc)[:150])
        self.universe_count=len(tickers)
        updates=[]
        if self.config.version=='3.0':
            eligible=sorted(tickers,key=lambda m:tickers[m]['volume_24h_tl'],reverse=True)
            held={p[0] for p in self.positions()}
            updates=self.research.step(eligible,held,time.time())
            ready=[m for m in eligible if self.research.context(m,time.time()).get('fresh')]
            ready.sort(key=lambda m:self.research.data[m]['metrics']['return_1h'],reverse=True)
            # Every TL ticker is sampled; expensive depth is prioritised and explicitly bounded.
            rotating=(eligible[self._book_rotation:]+eligible[:self._book_rotation])[:4]
            self._book_rotation=(self._book_rotation+4)%max(1,len(eligible))
            markets=held|set(ready[:6])|set(rotating)
        else:
            eligible = sorted((m for m in tickers if tickers[m]["volume_24h_tl"] >= 5000000 and m not in ("usdt_tl","usdc_tl")),key=lambda m:tickers[m]["volume_24h_tl"],reverse=True)[:10]
            markets = set(eligible)|{p[0] for p in self.positions()}
        if "btc_tl" in tickers:
            markets.add("btc_tl")
        books = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self.feed.book,m):m for m in markets}
            for future in concurrent.futures.as_completed(futures):
                market = futures[future]
                try:
                    books[market] = future.result()
                except Exception as exc:
                    errors.append(market+": "+type(exc).__name__+": "+str(exc)[:120])
        now = time.time()
        if entry_deadline is not None and now >= entry_deadline:
            allow_entries = False
            self.entries_open = False
        for market in list(books):
            if not -5 <= now-books[market]["timestamp"] <= 30:
                del books[market]
                errors.append(market+": expired during fetch")
        with self.db:
            if self.research:
                from strategy import compact
                for market,payload in updates:
                    self.db.execute("INSERT INTO research_snapshots VALUES(?,?,?)",(now,market,json.dumps(compact(payload))))
                    for interval,key in [('1h','hours'),('1d','days')]:
                        for candle in payload.get(key,[]):
                            self.db.execute("INSERT OR IGNORE INTO candles VALUES(?,?,?,?,?)",(market,payload['venue'],interval,candle['time'],json.dumps(candle)))
            for market,ticker in tickers.items():
                self.db.execute("INSERT OR IGNORE INTO ticker_observations VALUES(?,?,?,?)", (market,ticker_received,ticker["last"],ticker["volume_24h_tl"]))
            for market,book in books.items():
                self.record_book(market,book,now)
            for position in self.positions():
                if position[0] in books:
                    self.sell(position,books[position[0]],now)
                else:
                    self.decision(now,position[0],"HOLD","MISSING_EXIT_BOOK")
            held = {p[0] for p in self.positions()}
            candidates = []
            for market in eligible:
                if market in held:
                    continue
                reason,sig = None,None
                if not allow_entries:
                    reason = "ENTRY_WINDOW_CLOSED"
                elif market not in books:
                    reason = "MISSING_BOOK"
                else:
                    last_exit = self.db.execute("SELECT MAX(ts) FROM trades WHERE market=? AND side='SELL'",(market,)).fetchone()[0]
                    if last_exit is not None and now-last_exit < 1800:
                        reason = "COOLDOWN"
                    elif books[market]["asks"][0][0]/books[market]["bids"][0][0]-1 > .003:
                        reason = "SPREAD_TOO_WIDE"
                    else:
                        sig,reason = self.confirmed_signal(market,books[market],now)
                if self.config.version!='3.0':
                    self.db.execute("INSERT INTO shadow_signals VALUES(?,?,?,?)", (now,market,json.dumps(self.legacy_signal(market,now)),json.dumps(sig)))
                if sig:
                    candidates.append((market,sig))
                else:
                    self.decision(now,market,"SKIP",reason)
            if candidates:
                self.allocate(candidates,books,now)
            self.db.execute("INSERT INTO cycles VALUES(?,?,?)",(now,len(books),json.dumps(errors)))
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
                    net = gross*(1-self.config.fee)
            if net is None:
                complete = False
            else:
                value += net
            positions.append({"market":market,"quantity":qty,"cost_tl":round(cost,2),"estimated_net_sale_tl":round(net,2) if net is not None else None})
        cash = self.cash()
        equity = cash+value if complete else None
        counts = {m:len(self.closed_bars(m,now)) for m in eligible} if not self.research else {}
        ready = any(self.research.context(m,now).get('fresh') for m in eligible) if self.research else any(valid_history(self.closed_bars(m,now)) for m in eligible)
        coverage={m:{'status':self.research.context(m,now).get('status'),'fresh':self.research.context(m,now).get('fresh'),
                      'identity_verified':self.research.context(m,now).get('identity_verified',False)} for m in eligible} if self.research else {}
        return {"mode":"PAPER_ONLY","timestamp_utc":utc(now),"strategy_version":self.config.version,"entries_open":self.entries_open,
            "started_utc":self.db.execute("SELECT value FROM settings WHERE key='started'").fetchone()[0],
            "status":"DATA_ERROR" if not books else "EXIT_ONLY" if not self.entries_open else "WARMING_UP" if not ready else "MONITORING",
            "starting_capital_tl":CAPITAL,"cash_tl":round(cash,2),
            "estimated_equity_tl":round(equity,2) if equity is not None else None,
            "estimated_total_pnl_tl":round(equity-CAPITAL,2) if equity is not None else None,
            "realized_pnl_tl":round(self.db.execute("SELECT realized FROM account WHERE id=1").fetchone()[0],2),
            "trade_events":self.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0],
            "positions":positions,"valid_books":len(books),"closed_bid_quote_minutes":counts,
            "required_consecutive_minutes":None if self.research else WARMUP,"assumed_fee_each_side":self.config.fee,"config":asdict(self.config),
            "stop_net_return":STOP,"target_net_return":TARGET,"errors":errors,
            "threshold_mode":"REVIEW_POINTS" if self.research else "FIXED_EXITS",
            "hard_stop_net_return":self.config.hard_stop if self.research else STOP,
            "monitored_tl_markets":self.universe_count,"research_coverage":coverage,
            "chain_coverage":self.research.chain_data if self.research else None,
            "company_wallet_coverage":"NOT_VERIFIED","global_coverage":"PARTIAL_PUBLIC_DATA"}


def session_phase(now, entry_deadline, monitor_deadline, has_positions):
    if entry_deadline is None or now < entry_deadline:
        return "ENTRY"
    if not has_positions:
        return "COMPLETE"
    if monitor_deadline is not None and now >= monitor_deadline:
        return "PAUSED_OPEN_POSITIONS"
    return "EXIT_ONLY"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory",default=os.environ.get("BORU_DATA_DIR","data"))
    parser.add_argument("--duration-minutes",type=float,default=180,help="New-entry window; 0 for continuous service")
    parser.add_argument("--max-monitor-minutes",type=float,default=0,help="Exit-only window after entries close; 0 is unlimited")
    parser.add_argument("--resume-monitor-minutes",type=float,default=None,help="Explicitly extend monitoring of a paused session; never reopens entries")
    parser.add_argument("--interval",type=float,default=15)
    parser.add_argument("--cycles",type=int,default=0)
    parser.add_argument("--require-data",action="store_true")
    args = parser.parse_args()
    if args.interval < 10 or args.duration_minutes < 0 or args.cycles < 0 or args.max_monitor_minutes < 0 or (args.resume_monitor_minutes is not None and args.resume_monitor_minutes <= 0):
        parser.error("interval >=10; duration/cycles/monitor >=0; resume >0 required")
    sim,stopping = Simulation(args.directory),False
    saved = sim.db.execute("SELECT value FROM settings WHERE key='lifecycle'").fetchone()
    if saved:
        life = json.loads(saved[0])
    else:
        deadline = time.time()+args.duration_minutes*60 if args.duration_minutes else None
        life = dict(entry_deadline=deadline,monitor_deadline=deadline+args.max_monitor_minutes*60 if deadline and args.max_monitor_minutes else None)
    if args.resume_monitor_minutes is not None:
        if life['entry_deadline'] is None or time.time()<life['entry_deadline']:
            sim.close()
            parser.error("Resume is only for a session whose entry window has ended")
        life['monitor_deadline'] = time.time()+args.resume_monitor_minutes*60
    with sim.db:
        sim.db.execute("INSERT OR REPLACE INTO settings VALUES('lifecycle',?)", (json.dumps(life),))
    (sim.directory/"session_end.json").unlink(missing_ok=True)
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    count,valid,finish = 0,0,"INTERRUPTED"
    try:
        while not stopping:
            tick = time.monotonic()
            phase = session_phase(time.time(),life['entry_deadline'],life['monitor_deadline'],bool(sim.positions()))
            if phase in ("COMPLETE","PAUSED_OPEN_POSITIONS"):
                finish = phase
                break
            report = sim.cycle(allow_entries=phase=="ENTRY",entry_deadline=life["entry_deadline"])
            count += 1
            valid += int(report["valid_books"] > 0)
            if args.cycles and count >= args.cycles:
                finish = "OBSERVATION_LIMIT"
                break
            delay = max(0,args.interval-(time.monotonic()-tick))
            if not report["valid_books"] or any("429" in e for e in report["errors"]):
                delay = max(delay,60)
            deadline = time.monotonic()+delay
            while not stopping and time.monotonic() < deadline:
                time.sleep(min(1,max(0,deadline-time.monotonic())))
    finally:
        state = {"mode":"PAPER_ONLY","status":finish,"monitoring_active":False,
                 "stopped_utc":utc(),"open_positions":len(sim.positions()),
                 "requires_resume":bool(sim.positions()),"lifecycle":life}
        (sim.directory/"session_end.json").write_text(json.dumps(state,indent=2),encoding="utf-8")
        status_path = sim.directory/"status.json"
        if status_path.exists():
            status = json.loads(status_path.read_text())
            status.update(session_state=state)
            status_path.write_text(json.dumps(status,indent=2),encoding="utf-8")
        sim.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        sim.close()
    if args.require_data and valid == 0:
        raise SystemExit("No valid live market data; simulation did not start")


if __name__ == "__main__":
    main()
