import json
import tempfile
import unittest
from pathlib import Path
from boru import Simulation
from test_boru import book
from reporting import generate,records


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.sim=Simulation(self.tmp.name)

    def tearDown(self):
        self.sim.db.close()
        self.tmp.cleanup()

    def seed(self):
        for i in range(80):
            ts=1800000000+i*15
            self.sim.record_book('btc_tl',book(bid=99+i*.02,ask=99.05+i*.02,now=ts),ts)
        self.sim.buy('btc_tl',book(now=1800000600),10000,1800000600,{'rsi':62,'ema10':100.1,'ema30':99.8})
        self.sim.sell(self.sim.positions()[0],book(bid=104,ask=104.1,now=1800001200),1800001200)
        self.sim.db.commit()

    def test_replay_matches_final_ledger_and_no_fake_buy_profit(self):
        self.seed()
        r=records(self.sim.db)
        self.assertIsNone(r[0]['net_return_pct'])
        self.assertAlmostEqual(r[-1]['cash_after_tl'],self.sim.cash())
        self.assertAlmostEqual(r[-1]['realized_total_tl'],self.sim.db.execute('SELECT realized FROM account').fetchone()[0])
        self.assertGreater(r[-1]['net_return_pct'],3)
        self.assertAlmostEqual(r[0]['stop_reference_price'],100*1.0035*.98/.9965)

    def test_partial_sell_replay(self):
        self.sim.buy('btc_tl',book(),10000,1000,{})
        self.sim.sell(self.sim.positions()[0],book(bid=90,ask=90.1,quantity=10),1020)
        self.sim.db.commit()
        r=records(self.sim.db)
        self.assertAlmostEqual(r[-1]['remaining_qty'],self.sim.positions()[0][1])
        self.assertAlmostEqual(r[-1]['cash_after_tl'],self.sim.cash())

    def test_empty_session_does_not_create_fake_trade(self):
        idx=generate(self.tmp.name)
        self.assertEqual(idx['trades'],[])
        self.assertEqual(list((Path(self.tmp.name)/'trade_reports').glob('*.png')),[])

    def test_png_reports_are_idempotent_and_separate(self):
        self.seed()
        idx=generate(self.tmp.name)
        self.assertEqual(len(idx['trades']),2)
        output=Path(self.tmp.name)/'trade_reports'
        first=output/idx['trades'][0]['png']
        self.assertEqual(first.read_bytes()[:8],b'\x89PNG\r\n\x1a\n')
        previous=first.stat().st_mtime_ns
        self.assertEqual(generate(self.tmp.name),idx)
        self.assertEqual(first.stat().st_mtime_ns,previous)
        report=json.loads((output/idx['trades'][1]['json']).read_text())
        self.assertEqual(report['mode'],'PAPER_ONLY')
        self.assertTrue(report['timestamp_local'].endswith('+03:00'))


if __name__=='__main__':
    unittest.main()
