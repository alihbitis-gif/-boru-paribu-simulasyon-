"""Synthetic agent responses test boundaries, not live Grok performance."""
import copy
import os
import time
import unittest
from unittest.mock import patch
from grok_team import BOTS, GrokClient, GrokTeam, validate_result, risk_review


def packet():
    now=time.time()
    return {'packet_id':'p','market':'btc_tl','created_at':now,'valid_until':now+120,
            'research_fresh':True,'evidence':[{'evidence_id':'e'}],
            'portfolio':{'positions':[]},'mode':'PAPER_REVIEW_ONLY'}


def response(role):
    data={'packet_id':'p','market':'btc_tl','evidence_ids':['e'],'reason_tr':'Sentetik test.', 'limitations_tr':[]}
    if role=='decision':data.update(action='HOLD',budget_fraction=0)
    else:data['assessment']='MIXED'
    return data


class GrokTests(unittest.TestCase):
    def test_three_bots_four_roles(self):
        self.assertEqual(len(BOTS),3)
        self.assertEqual(sum(map(len,BOTS.values())),4)

    def test_missing_key_blocks_without_network(self):
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaisesRegex(ValueError,'CONNECTION_REQUIRED'):GrokClient()

    def test_unknown_evidence_is_rejected(self):
        r=response('decision');r['evidence_ids']=['invented']
        with self.assertRaises(ValueError):validate_result('decision',r,packet())

    def test_wrong_packet_is_rejected(self):
        r=response('decision');r['packet_id']='other'
        with self.assertRaises(ValueError):validate_result('decision',r,packet())

    def test_invalid_budget_or_extra_action_is_rejected(self):
        for fraction in (float('nan'),2,-1,True):
            r=response('decision');r.update(action='BUY',budget_fraction=fraction)
            with self.assertRaises(ValueError):validate_result('decision',r,packet())
        r=response('decision');r['place_real_order']=True
        with self.assertRaises(ValueError):validate_result('decision',r,packet())

    def test_stale_buy_and_unowned_sell_are_rejected(self):
        p=packet();p['research_fresh']=False
        r=response('decision');r.update(action='BUY',budget_fraction=.1)
        self.assertFalse(risk_review(r,p)['proposal_usable_for_review'])
        r.update(action='SELL',budget_fraction=0)
        self.assertFalse(risk_review(r,p)['proposal_usable_for_review'])

    def test_expired_packet_is_rejected(self):
        p=packet();p['valid_until']=p['created_at']-1
        self.assertFalse(risk_review(response('decision'),p)['proposal_usable_for_review'])

    def test_pipeline_is_review_only_even_after_success(self):
        class Client:
            model='TEST_FAKE_NOT_LIVE'
            def review(self,role,packet,analyses=None):return response(role)
        output=GrokTeam(Client()).analyze(packet())
        self.assertEqual(set(output['analyses']),{'technical','chain'})
        self.assertFalse(output['risk_review']['can_execute'])

    def test_unverified_report_cannot_be_explained(self):
        with self.assertRaises(ValueError):GrokTeam(object()).explain_report(packet())


if __name__=='__main__':unittest.main()
