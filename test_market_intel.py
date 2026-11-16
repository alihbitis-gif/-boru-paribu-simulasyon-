"""Synthetic fixtures exercise accounting/decisions, never represent a live result."""
import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from boru import Simulation,Config,validate_tickers
from market_intel import candles,analyze_candles,large_trades,swap_event,SWAP_TOPIC
from strategy import entry_decision,exit_decision
from test_boru import book


def context():
    return {'status':'READY','fresh':True,'identity_verified':True,'trade_feed_fresh':True,'received':10200,
        'venue':'Binance','quote':'USDT','metrics':{'return_1h':.02,'return_24h':.04,'return_7d':.10,
            'hour_trend_up':True,'daily_trend_up':True,'atr_fraction':.01,'volume_ratio':1.2,
            'last_closed_price':2.5,'patterns':{'bullish_engulfing':False,'bearish_engulfing':False,'hammer':False,'shooting_star':False,'doji':False}},
        'large_trades':{'buy':0,'sell':0,'events':[],'owner_verified':False},
        'chain':{'fresh':True,'status':'READY','buy':0,'sell':0,'scope':'test fixture'}}


class MarketIntelTests(unittest.TestCase):
    def test_open_candle_excluded_and_gaps_rejected(self):
        rows=[[i*3600000,100,102,99,101,10,(i+1)*3600000-1,1000,10,0,0] for i in range(4)]
        self.assertEqual(len(candles(rows,3600,3*3600+10)),3)
        with self.assertRaises(ValueError):candles([rows[0],rows[2]],3600,3*3600+10)
        bad=copy.deepcopy(rows);bad[0][2]=98
        with self.assertRaises(ValueError):candles(bad,3600,3*3600+10)

    def test_hour_day_week_returns_use_real_ohlcv_fields(self):
        h=[{'time':i*3600,'end':(i+1)*3600-.001,'open':100+i,'high':102+i,'low':99+i,'close':101+i,'volume':10+i} for i in range(168)]
        d=[{'time':i*86400,'end':(i+1)*86400-.001,'open':100+i,'high':102+i,'low':99+i,'close':101+i,'volume':100+i} for i in range(8)]
        m=analyze_candles(h,d)
        self.assertAlmostEqual(m['return_7d'],268/100-1)
        self.assertAlmostEqual(m['return_24h'],268/244-1)
        self.assertAlmostEqual(m['return_1h'],268/267-1)
        with self.assertRaises(ValueError):analyze_candles(h[:50],d)

    def test_taker_direction_not_maker_direction(self):
        rows=[{'a':1,'T':1000000,'p':'100','q':'1200','m':True},
              {'a':2,'T':1000000,'p':'100','q':'1500','m':False}]
        flow=large_trades(rows+rows,1000)
        self.assertEqual(flow['sell'],120000)
        self.assertEqual(flow['buy'],150000)
        self.assertEqual(len(flow['events']),2)
        self.assertFalse(flow['owner_verified'])
        self.assertFalse(flow['complete_5m_window'])
        self.assertEqual(large_trades(rows,1400)['events'],[])

    def test_transfer_is_not_a_swap_and_removed_logs_are_ignored(self):
        pool='0x'+'1'*40
        word=lambda n:hex(n%(1<<256))[2:].zfill(64)
        log={'address':pool,'topics':[SWAP_TOPIC,'0x'+'0'*64,'0x'+'1'*64],
             'data':'0x'+word(200000*10**6)+word(-100*10**18)+word(1)+word(2)+word(3),
             'blockNumber':'0x10','transactionHash':'0x'+'a'*64,'logIndex':'0x0','removed':False}
        event=swap_event(log,pool)
        self.assertEqual(event['side'],'BUY')
        self.assertEqual(event['notional'],200000)
        self.assertEqual(event['owner'],'unknown')
        log['removed']=True
        self.assertIsNone(swap_event(log,pool))
        log['removed']=False;log['topics'][0]='0x'+'0'*64
        self.assertIsNone(swap_event(log,pool))

    def test_all_valid_tl_tickers_include_zero_volume(self):
        ticks=validate_tickers([{'market':'btc_tl','last':100,'pair_volume':0},{'market':'new_tl','last':1,'pair_volume':5},{'market':'eth_usdt','last':100,'pair_volume':10}])
        self.assertEqual(set(ticks),{'btc_tl','new_tl'})

    def test_large_sell_requires_price_recovery_and_confirmation(self):
        c=context();c['large_trades']['sell']=200000
        self.assertEqual(entry_decision(c,True),(True,'LARGE_SELL_ABSORPTION'))
        self.assertFalse(entry_decision(c,False)[0])
        c['metrics']['patterns']['bearish_engulfing']=True
        self.assertFalse(entry_decision(c,True)[0])

    def test_unknown_identity_and_old_tape_cannot_enter(self):
        c=context();c['identity_verified']=False
        self.assertFalse(entry_decision(c,True)[0])
        c=context();c['trade_feed_fresh']=False
        self.assertFalse(entry_decision(c,True)[0])

    def test_minus_two_is_review_not_unconditional_sale(self):
        c=context()
        self.assertEqual(exit_decision(-.021,0,c),(False,'HOLD_RECOVERY_WITH_CHAIN_CONFIRMATION'))
        c['chain']['fresh']=False
        self.assertEqual(exit_decision(-.021,0,c),(True,'LOSS_EXIT_CHAIN_UNCONFIRMED'))
        c=context();c['metrics']['hour_trend_up']=False
        self.assertTrue(exit_decision(-.021,0,c)[0])

    def test_plus_three_can_hold_and_profit_can_exit_earlier(self):
        self.assertEqual(exit_decision(.035,.035,context()),(False,'HOLD_UPSIDE'))
        c=context();c['metrics']['patterns']['shooting_star']=True
        self.assertEqual(exit_decision(.008,.008,c),(True,'EARLY_PROFIT_WEAKENING'))
        c=context();c['large_trades']['buy']=200000
        self.assertEqual(exit_decision(.01,.01,c),(True,'LARGE_BUY_COUNTERTRADE_PROFIT'))

    def test_trailing_profit_and_hard_risk_limit(self):
        self.assertEqual(exit_decision(.018,.03,context()),(True,'TRAILING_PROFIT'))
        self.assertEqual(exit_decision(-.06,0,{}),(True,'HARD_RISK_EXIT'))
        self.assertEqual(exit_decision(-.025,0,{}),(True,'LOSS_EXIT_DATA_UNCONFIRMED'))

    def test_v3_cycle_monitors_every_tl_symbol_and_saves_research(self):
        class Research:
            def __init__(self):self.data={};self.chain_data={'status':'READY'}
            def step(self,markets,held,now):
                self.data={m:context() for m in markets}
                return [(m,context()) for m in markets]
            def context(self,market,now):return self.data.get(market,context())
            def close(self):pass
        class Feed:
            def tickers(self):
                return {**{f'asset{i}_tl':{'last':100,'volume_24h_tl':i} for i in range(25)},'usdt_tl':{'last':40,'volume_24h_tl':100}}
            def book(self,market):return book(bid=99.9,ask=100,now=10200)
        with tempfile.TemporaryDirectory() as d:
            sim=Simulation(d,feed=Feed())
            sim.research.close();sim.research=Research()
            try:
                for i in range(25):sim.db.execute('INSERT INTO ticker_observations VALUES(?,?,?,?)',(f'asset{i}_tl',10180,99,0))
                with patch('boru.time.time',return_value=10200),patch('builtins.print'):
                    result=sim.cycle()
                self.assertEqual(result['monitored_tl_markets'],26)
                self.assertEqual(len(result['research_coverage']),26)
                self.assertEqual(result['threshold_mode'],'REVIEW_POINTS')
                self.assertGreater(result['trade_events'],0)
                self.assertGreater(sim.cash(),12000)
                self.assertEqual(sim.db.execute('SELECT COUNT(*) FROM research_snapshots').fetchone()[0],26)
            finally:sim.close()

    def test_v3_exit_records_decision_and_context(self):
        class Research:
            def context(self,market,now):return context()
            def close(self):pass
        with tempfile.TemporaryDirectory() as d:
            sim=Simulation(d)
            sim.research.close();sim.research=Research()
            try:
                sim.buy('eth_tl',book(),3000,1000,{'decision':'MULTITIMEFRAME_TREND'})
                # Gross move >3% with fresh supportive data is held.
                sim.sell(sim.positions()[0],book(bid=104,ask=104.1,now=1020),1020)
                self.assertEqual(len(sim.positions()),1)
                sim.sell(sim.positions()[0],book(bid=90,ask=90.1,now=1040),1040)
                self.assertEqual(sim.positions(),[])
                row=sim.db.execute('SELECT payload FROM execution_context ORDER BY trade_id DESC LIMIT 1').fetchone()
                self.assertEqual(json.loads(row[0])['decision'],'HARD_RISK_EXIT')
            finally:sim.close()


if __name__=='__main__':unittest.main()
