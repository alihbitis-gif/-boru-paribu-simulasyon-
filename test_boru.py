import math
import tempfile
import unittest
from unittest.mock import patch
from boru import Simulation,Config,MarketData,validate_tickers,validate_book,sweep,signal_for,session_phase,STOP,TARGET,FEE


def book(bid=99.9,ask=100,now=1000,quantity=100000):
    return validate_book({"timestamp":now,"bids":[[bid,quantity]],"asks":[[ask,quantity]]},now=now)


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sim = Simulation(self.tmp.name,config=Config(version="2.0"))

    def tearDown(self):
        self.sim.close()
        self.tmp.cleanup()

    def test_round_trip_costs_and_target(self):
        self.assertTrue(self.sim.buy("btc_tl",book(),20000,1000,{}))
        self.assertAlmostEqual(self.sim.cash(),0,places=7)
        self.sim.sell(self.sim.positions()[0],book(bid=104,ask=104.1,now=1020),1020)
        self.assertEqual(self.sim.positions(),[])
        self.assertAlmostEqual(self.sim.cash(),20000/100*(1-FEE)*104*(1-FEE))
        self.assertGreaterEqual(self.sim.cash()/20000-1,TARGET)

    def test_stop_gap_uses_executable_price(self):
        self.sim.buy("btc_tl",book(),10000,1000,{})
        self.sim.sell(self.sim.positions()[0],book(bid=95,ask=95.1,now=1020),1020)
        self.assertEqual(len(self.sim.positions()),0)
        pnl = self.sim.db.execute("SELECT pnl FROM trades WHERE side='SELL'").fetchone()[0]
        self.assertLess(pnl/10000,STOP)

    def test_no_sale_between_thresholds(self):
        self.sim.buy("btc_tl",book(),10000,1000,{})
        self.sim.sell(self.sim.positions()[0],book(bid=100,ask=100.1,now=1020),1020)
        self.assertEqual(len(self.sim.positions()),1)

    def test_partial_exit_conserves_cost(self):
        self.sim.buy("btc_tl",book(),10000,1000,{})
        before = self.sim.positions()[0]
        self.sim.sell(before,book(bid=90,ask=90.1,quantity=10,now=1020),1020)
        after = self.sim.positions()[0]
        self.assertAlmostEqual(before[1]-after[1],1)
        self.assertAlmostEqual(after[2],before[2]*(before[1]-1)/before[1])

    def test_invalid_books_rejected(self):
        for data in [
            {"timestamp":1000,"bids":[[99,1]],"asks":[[100,1]]},
            {"timestamp":1100,"bids":[[math.nan,1]],"asks":[[100,1]]},
            {"timestamp":1100,"bids":[[101,1]],"asks":[[100,1]]},
        ]:
            with self.assertRaises(ValueError):
                validate_book(data,now=1100)

    def test_depth_limited_fill(self):
        self.assertEqual(sweep([(100,10),(101,20)],budget=10000),(3,302))

    def test_missing_minutes_and_duplicate_quotes(self):
        self.assertIsNone(signal_for([]))
        bars = [{"minute":i*60,"high":100+i/100,"close":100+i/100,"samples":4} for i in range(60)]
        bars[30]["minute"] += 60
        self.assertIsNone(signal_for(bars))
        self.sim.record_book("btc_tl",book(),1000)
        self.sim.record_book("btc_tl",book(),1000)
        self.assertEqual(self.sim.db.execute("SELECT samples FROM bars").fetchone()[0],1)

    def test_restart_preserves_state(self):
        self.sim.buy("btc_tl",book(),5000,1000,{})
        self.sim.db.commit()
        self.sim.close()
        self.sim = Simulation(self.tmp.name,config=Config(version="2.0"))
        other = self.sim
        self.assertAlmostEqual(other.cash(),15000)
        self.assertEqual(len(other.positions()),1)

    def test_unknown_valuation_is_not_fabricated(self):
        self.sim.buy("btc_tl",book(),5000,1000,{})
        self.assertIsNone(self.sim.report(1000,{},[],[])["estimated_equity_tl"])

    def test_whole_cycle_enters_only_after_valid_closed_history(self):
        now = 10200
        prices = [100+i*.02+.09*math.sin(i*1.5) for i in range(60)]
        prices[-1] = max(prices[-21:-1])+.16
        for i,price in enumerate(prices):
            minute = now-3600+i*60
            self.sim.db.execute("INSERT INTO bars VALUES(?,?,?,?,?,?)",("btc_tl",minute,price,price,price,4))
        class Feed:
            def tickers(self):
                return {"btc_tl":{"volume_24h_tl":10000000,"last":prices[-1]}}
            def book(self,market):
                return book(bid=prices[-1],ask=prices[-1]+.02,now=now)
        self.sim.db.execute("INSERT INTO ticker_observations VALUES(?,?,?,?)", ("btc_tl",now-45,prices[-2],10000000))
        self.sim.feed = Feed()
        with patch("boru.time.time",return_value=now),patch("builtins.print"):
            result = self.sim.cycle()
        self.assertEqual(result["trade_events"],1)
        self.assertEqual(len(self.sim.positions()),1)
        self.assertGreaterEqual(self.sim.cash(),-1e-8)

    def test_data_failure_keeps_cash_and_records_error(self):
        class Feed:
            def tickers(self):
                raise ValueError("feed unavailable")
        self.sim.feed = Feed()
        with patch("builtins.print"):
            result = self.sim.cycle()
        self.assertEqual(result["status"],"DATA_ERROR")
        self.assertEqual(result["trade_events"],0)
        self.assertEqual(result["cash_tl"],20000)
        self.assertIn("feed unavailable",result["errors"][0])

    def test_buy_fee_is_asset_not_double_cash(self):
        self.assertTrue(self.sim.buy("trx_tl",book(),10000,1000,{}))
        market,qty,cost,_ = self.sim.positions()[0]
        self.assertAlmostEqual(qty,100*(1-FEE))
        self.assertAlmostEqual(cost,10000)
        self.assertAlmostEqual(self.sim.cash(),10000)
        fee = self.sim.db.execute("SELECT fee_currency,fee_quantity,notional FROM fills").fetchone()
        self.assertEqual(fee[0],"TRX")
        self.assertAlmostEqual(fee[1],100*FEE)
        self.assertAlmostEqual(fee[2],10000)

    def test_high_cost_and_shallow_exit_reject_entry(self):
        self.assertFalse(self.sim.buy("trx_tl",book(bid=99.5),10000,1000,{}))
        b=book(); b["bids"]=[[99.9,1]]
        self.assertFalse(self.sim.buy("trx_tl",b,10000,1000,{}))
        self.assertEqual(self.sim.cash(),20000)
        self.assertEqual(len(self.sim.positions()),0)

    def test_risk_budget_reserves_cash_and_caps_total_exposure(self):
        books={m:book() for m in ("a_tl","b_tl","c_tl")}
        self.sim.allocate([(m,{"score":i+1}) for i,m in enumerate(books)],books,1000)
        self.assertAlmostEqual(self.sim.cash(),4000)
        self.assertEqual(len(self.sim.positions()),2)
        self.assertTrue(all(p[2] <= 8000+1e-8 for p in self.sim.positions()))
        self.assertLessEqual(sum(p[2]*.025 for p in self.sim.positions()),400+1e-8)

    def test_partial_stop_remains_latched_but_cannot_reuse_liquidity(self):
        self.sim.buy("btc_tl",book(),10000,1000,{})
        low=book(bid=90,ask=90.1,quantity=10,now=1020)
        self.sim.sell(self.sim.positions()[0],low,1020)
        qty=self.sim.positions()[0][1]
        self.sim.sell(self.sim.positions()[0],low,1021)
        self.assertAlmostEqual(self.sim.positions()[0][1],qty)
        self.sim.sell(self.sim.positions()[0],book(bid=100,ask=100.1,now=1040),1040)
        self.assertEqual(self.sim.positions(),[])
        self.assertEqual(self.sim.db.execute("SELECT reason FROM trades ORDER BY id DESC LIMIT 1").fetchone()[0],"STOP")

    def test_stale_exit_does_not_pretend_stop_filled(self):
        self.sim.buy("btc_tl",book(),10000,1000,{})
        self.sim.sell(self.sim.positions()[0],book(bid=90,ask=90.1),1100)
        self.assertEqual(len(self.sim.positions()),1)
        self.assertEqual(self.sim.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0],1)

    def test_ask_only_move_does_not_raise_bid_bar(self):
        self.sim.record_book("trx_tl",book(bid=99,ask=100,now=1000),1000)
        self.sim.record_book("trx_tl",book(bid=99,ask=105,now=1005),1005)
        high,close=self.sim.db.execute("SELECT high,close FROM bars").fetchone()
        self.assertEqual((high,close),(99,99))
        self.assertEqual(self.sim.db.execute("SELECT COUNT(*) FROM books").fetchone()[0],2)

    def test_window_does_not_abandon_open_position_at_entry_deadline(self):
        self.assertEqual(session_phase(99,100,200,False),"ENTRY")
        self.assertEqual(session_phase(100,100,200,True),"EXIT_ONLY")
        self.assertEqual(session_phase(200,100,200,True),"PAUSED_OPEN_POSITIONS")
        self.assertEqual(session_phase(200,100,None,True),"EXIT_ONLY")
        self.assertEqual(session_phase(100,100,200,False),"COMPLETE")

    def test_duplicate_process_is_rejected(self):
        self.sim.db.commit()
        with self.assertRaisesRegex(RuntimeError,"Another simulation"):
            Simulation(self.tmp.name,config=Config(version="2.0"))

    def test_config_cannot_silently_reprice_existing_session(self):
        self.sim.close()
        with self.assertRaisesRegex(ValueError,"immutable"):
            Simulation(self.tmp.name,config=Config(version="2.0",fee=.0035))
        self.sim=Simulation(self.tmp.name,config=Config(version="2.0"))
        self.assertEqual(self.sim.config.fee,FEE)

    def test_ticker_parser_and_public_endpoint_allowlist(self):
        self.assertEqual(validate_tickers([{"market":"trx_tl","last":"15.4","pair_volume":"10000000"}])["trx_tl"]["last"],15.4)
        with self.assertRaises(ValueError):
            validate_tickers([{"market":"trx_tl","last":"nan","pair_volume":"1"}])
        for endpoint in ("/order","/user/assets","/trades/history"):
            with self.assertRaises(ValueError):
                MarketData().get(endpoint)

    def seed_confirmable_history(self, now=10200):
        prices=[100+i*.02+.09*math.sin(i*1.5) for i in range(60)]
        prices[-1]=max(prices[-21:-1])+.16
        for i,price in enumerate(prices):
            self.sim.db.execute("INSERT INTO bars VALUES(?,?,?,?,?,?)",("btc_tl",now-3600+i*60,price,price,price,4))
        return prices

    def test_last_price_confirmation_is_required_even_for_valid_breakout(self):
        prices=self.seed_confirmable_history()
        b=book(bid=prices[-1],ask=prices[-1]+.02,now=10200)
        sig,reason=self.sim.confirmed_signal("btc_tl",b,10200)
        self.assertIsNone(sig)
        self.assertEqual(reason,"LAST_TRADE_PRICE_UNCONFIRMED")
        for t in (10155,10200):
            self.sim.db.execute("INSERT INTO ticker_observations VALUES(?,?,?,?)",("btc_tl",t,prices[-1],10000000))
        self.assertIsNone(self.sim.confirmed_signal("btc_tl",b,10200)[0])
        self.sim.db.execute("UPDATE ticker_observations SET last=? WHERE received=10155",(prices[-2],))
        self.assertIsNotNone(self.sim.confirmed_signal("btc_tl",b,10200)[0])

    def test_entry_window_closed_still_executes_stop(self):
        self.sim.buy("btc_tl",book(),8000,1000,{})
        class Feed:
            def tickers(self):
                return {}
            def book(self,market):
                return book(bid=90,ask=90.1,now=1020)
        self.sim.feed=Feed()
        with patch("boru.time.time",return_value=1020),patch("builtins.print"):
            result=self.sim.cycle(allow_entries=False)
        self.assertEqual(result["trade_events"],2)
        self.assertFalse(result["entries_open"])
        self.assertEqual(self.sim.positions(),[])

    def test_unknown_held_valuation_blocks_additional_exposure(self):
        self.sim.buy("btc_tl",book(),8000,1000,{})
        self.sim.allocate([("trx_tl",{"score":1})],{"trx_tl":book(now=1020)},1020)
        self.assertEqual(len(self.sim.positions()),1)
        self.assertEqual(self.sim.db.execute("SELECT reason FROM decisions ORDER BY rowid DESC LIMIT 1").fetchone()[0],"UNKNOWN_PORTFOLIO_VALUE")

    def test_legacy_database_refused_without_modifying_bytes(self):
        import sqlite3
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"simulation.sqlite3"
            db=sqlite3.connect(path)
            db.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)")
            db.execute("INSERT INTO settings VALUES('started','legacy')")
            db.commit();db.close()
            before=path.read_bytes()
            with self.assertRaisesRegex(ValueError,"Legacy"):
                Simulation(folder)
            self.assertEqual(path.read_bytes(),before)

    def test_checkpoint_can_restore_exact_account_and_pending_exit(self):
        from checkpoint import checkpoint
        self.sim.buy("btc_tl",book(),8000,1000,{})
        self.sim.sell(self.sim.positions()[0],book(bid=90,ask=90.1,quantity=10,now=1020),1020)
        self.sim.db.execute("INSERT INTO settings VALUES('lifecycle',?)", ('{"entry_deadline":1000,"monitor_deadline":2000}',))
        self.sim.db.commit()
        with tempfile.TemporaryDirectory() as output:
            checkpoint(self.tmp.name,output)
            restored=Simulation(output)
            try:
                self.assertEqual(restored.positions(),self.sim.positions())
                self.assertAlmostEqual(restored.cash(),self.sim.cash())
                self.assertEqual(restored.db.execute("SELECT reason FROM exit_intents").fetchone()[0],"STOP")
                self.assertEqual(restored.db.execute("SELECT value FROM settings WHERE key='lifecycle'").fetchone()[0],'{"entry_deadline":1000,"monitor_deadline":2000}')
            finally:
                restored.close()

    def test_expired_entry_deadline_overrides_entry_flag_during_fetch(self):
        class Feed:
            def tickers(self):
                return {"btc_tl":{"volume_24h_tl":10000000,"last":100}}
            def book(self,market):
                return book(now=1020)
        self.sim.feed=Feed()
        with patch("boru.time.time",return_value=1020),patch("builtins.print"):
            result=self.sim.cycle(allow_entries=True,entry_deadline=1010)
        self.assertFalse(result["entries_open"])
        self.assertEqual(result["trade_events"],0)
        self.assertEqual(self.sim.db.execute("SELECT reason FROM decisions ORDER BY rowid DESC LIMIT 1").fetchone()[0],"ENTRY_WINDOW_CLOSED")


if __name__ == "__main__":
    unittest.main()
