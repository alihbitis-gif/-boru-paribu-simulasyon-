import math
import tempfile
import unittest
from unittest.mock import patch
from boru import Simulation,validate_book,sweep,signal_for,STOP,TARGET


def book(bid=99.9,ask=100,now=1000,quantity=100000):
    return validate_book({"timestamp":now,"bids":[[bid,quantity]],"asks":[[ask,quantity]]},now=now)


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sim = Simulation(self.tmp.name)

    def tearDown(self):
        self.sim.db.close()
        self.tmp.cleanup()

    def test_round_trip_costs_and_target(self):
        self.assertTrue(self.sim.buy("btc_tl",book(),20000,1000,{}))
        self.assertAlmostEqual(self.sim.cash(),0,places=7)
        self.sim.sell(self.sim.positions()[0],book(bid=104,ask=104.1),1020)
        self.assertEqual(self.sim.positions(),[])
        self.assertAlmostEqual(self.sim.cash(),20000/1.0035/100*104*0.9965)
        self.assertGreaterEqual(self.sim.cash()/20000-1,TARGET)

    def test_stop_gap_uses_executable_price(self):
        self.sim.buy("btc_tl",book(),10000,1000,{})
        self.sim.sell(self.sim.positions()[0],book(bid=95,ask=95.1),1020)
        self.assertEqual(len(self.sim.positions()),0)
        pnl = self.sim.db.execute("SELECT pnl FROM trades WHERE side='SELL'").fetchone()[0]
        self.assertLess(pnl/10000,STOP)

    def test_no_sale_between_thresholds(self):
        self.sim.buy("btc_tl",book(),10000,1000,{})
        self.sim.sell(self.sim.positions()[0],book(bid=100,ask=100.1),1020)
        self.assertEqual(len(self.sim.positions()),1)

    def test_partial_exit_conserves_cost(self):
        self.sim.buy("btc_tl",book(),10000,1000,{})
        before = self.sim.positions()[0]
        self.sim.sell(before,book(bid=90,ask=90.1,quantity=10),1020)
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
        other = Simulation(self.tmp.name)
        self.assertAlmostEqual(other.cash(),15000)
        self.assertEqual(len(other.positions()),1)
        other.db.close()

    def test_unknown_valuation_is_not_fabricated(self):
        self.sim.buy("btc_tl",book(),5000,1000,{})
        self.assertIsNone(self.sim.report(1000,{},[],[])["estimated_equity_tl"])

    def test_whole_cycle_enters_only_after_valid_closed_history(self):
        now = 10020
        prices = [100+i*.02+.07*math.sin(i*1.5) for i in range(60)]
        prices[-1] = max(prices[-21:-1])+.01
        for i,price in enumerate(prices):
            minute = now-3600+i*60
            self.sim.db.execute("INSERT INTO bars VALUES(?,?,?,?,?,?)",("btc_tl",minute,price,price,price,4))
        class Feed:
            def tickers(self):
                return {"btc_tl":10000000}
            def book(self,market):
                return book(bid=prices[-1],ask=prices[-1]+.02,now=now)
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


if __name__ == "__main__":
    unittest.main()
