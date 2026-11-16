"""Synthetic parser fixtures only; these are not user session results."""
import unittest
from unittest.mock import patch
from chains import ChainReader, ChainMonitor, parse_bitcoin, parse_solana, parse_tron, parse_xrpl
from market_intel import ResearchEngine
from strategy import exit_decision
from test_market_intel import context


class ChainTests(unittest.TestCase):
    def test_bitcoin_change_is_not_a_sell_and_coinbase_is_excluded(self):
        tx={'txid':'id','vin':[{}],'vout':[{'value':2_000_000_000,'scriptpubkey_address':'address'}],
            'status':{'confirmed':True,'block_hash':'hash'}}
        event=parse_bitcoin([tx],'hash')[0]
        self.assertEqual(event['side'],'UNKNOWN')
        self.assertTrue(event['may_be_change_or_self_transfer'])
        tx['vin'][0]['is_coinbase']=True
        self.assertEqual(parse_bitcoin([tx],'hash'),[])
        tx['vin']=[{}];tx['status']['confirmed']=False
        with self.assertRaises(ValueError):parse_bitcoin([tx],'hash')

    def test_solana_failed_transactions_are_not_transfers(self):
        instruction={'programId':'11111111111111111111111111111111',
                     'parsed':{'type':'transfer','info':{'lamports':2_000_000_000_000,'source':'a','destination':'b'}}}
        tx={'meta':{'err':None,'innerInstructions':[]},'transaction':{'signatures':['sig'],
            'message':{'instructions':[instruction]}}}
        self.assertEqual(parse_solana([tx])[0]['quantity'],2000)
        tx['meta']['err']={'InstructionError':[0,'failed']}
        self.assertEqual(parse_solana([tx]),[])
        tx['meta']['err']=None;instruction['programId']='unrecognized'
        self.assertEqual(parse_solana([tx]),[])

    def test_tron_failed_or_token_contracts_are_not_native_transfers(self):
        tx={'txID':'id','ret':[{'contractRet':'SUCCESS'}], 'raw_data':{'contract':[
            {'type':'TransferContract','parameter':{'value':{'amount':2_000_000_000_000}}}]}}
        self.assertEqual(parse_tron([tx])[0]['quantity'],2_000_000)
        tx['ret'][0]['contractRet']='REVERT'
        self.assertEqual(parse_tron([tx]),[])
        tx['ret'][0]['contractRet']='SUCCESS';tx['raw_data']['contract'][0]['type']='TriggerSmartContract'
        self.assertEqual(parse_tron([tx]),[])

    def test_xrpl_uses_delivered_not_requested_partial_payment(self):
        tx={'hash':'id','TransactionType':'Payment','Amount':'999999999999999',
            'metaData':{'TransactionResult':'tesSUCCESS','delivered_amount':'1'}}
        self.assertEqual(parse_xrpl([tx]),[])
        tx['metaData']['delivered_amount']='200000000000'
        self.assertEqual(parse_xrpl([tx])[0]['quantity'],200000)
        tx['metaData']['TransactionResult']='tecPATH_DRY'
        self.assertEqual(parse_xrpl([tx]),[])

    def test_wrong_network_is_rejected(self):
        reader=ChainReader(transport=lambda *a,**kw:{'result':'0x61'})
        self.assertEqual(reader.snapshot('bnb')['error'],'MAINNET_IDENTITY_UNCONFIRMED')

    def test_solana_version_one_blocks_are_requested_explicitly(self):
        reader=ChainReader()
        with patch.object(reader,'rpc',side_effect=[100,{'blockhash':'x','blockTime':1000,'transactions':[]}]) as rpc:
            reader.solana()
            self.assertEqual(rpc.call_args.args[2][1]['maxSupportedTransactionVersion'],1)

    def test_rpc_write_methods_cannot_be_called(self):
        reader=ChainReader(transport=lambda *a,**kw:self.fail('network must not be reached'))
        with self.assertRaises(ValueError):reader.rpc('ethereum','eth_sendRawTransaction',[])

    def test_stale_data_is_not_ready(self):
        reader=ChainReader(clock=lambda:10000)
        reader.verified.add('bnb')
        with patch.object(reader,'evm',return_value={'height':1,'block_hash':'x','block_time':1,'events':[]}):
            self.assertEqual(reader.snapshot('bnb')['error'],'STALE_OR_FUTURE_BLOCK')

    def test_reverted_evm_value_is_not_recorded(self):
        reader=ChainReader()
        block={'number':'0x1','hash':'hash','timestamp':'0x1','transactions':[{'value':hex(200*10**18),'hash':'tx'}]}
        with patch.object(reader,'rpc',side_effect=[block,{'blockHash':'hash','status':'0x0'}]):
            self.assertEqual(reader.evm('ethereum')['events'],[])

    def test_transfer_coverage_cannot_authorize_holding_below_minus_two(self):
        c=context();c['chain']['fresh']=False
        c['chain_activity']={'status':'READY','fresh':True,'trade_direction_supported':False}
        self.assertEqual(exit_decision(-.025,0,c),(True,'LOSS_EXIT_CHAIN_UNCONFIRMED'))

    def test_transfer_errors_are_not_mislabeled_as_zero_flow(self):
        reader=ChainReader(transport=lambda *a,**kw:(_ for _ in ()).throw(ValueError('HTTP_429')))
        result=reader.snapshot('ethereum')
        self.assertEqual(result['status'],'DATA_ERROR')
        self.assertNotIn('events',result)


if __name__=='__main__':unittest.main()
