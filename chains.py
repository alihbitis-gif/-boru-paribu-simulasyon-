"""Six read-only mainnet adapters. Sampled transfers are NEVER buy/sell signals."""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

CHAINS = ('bitcoin', 'ethereum', 'solana', 'bnb', 'tron', 'xrpl')
ASSETS = dict(zip(CHAINS, ('BTC', 'ETH', 'SOL', 'BNB', 'TRX', 'XRP')))
# Native-unit observation filters, NOT verified whale identity or USD valuations.
THRESHOLDS = dict(zip(CHAINS, (10, 100, 1000, 100, 1_000_000, 100_000)))
DEFAULTS = {
    'bitcoin': 'https://blockstream.info/api',
    'ethereum': 'https://ethereum-rpc.publicnode.com',
    'solana': 'https://api.mainnet-beta.solana.com',
    'bnb': 'https://bsc-dataseed.bnbchain.org',
    'tron': 'https://api.trongrid.io',
    'xrpl': 'https://s1.ripple.com:51234',
}
BTC_GENESIS = '000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f'
SOL_GENESIS = '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d'
READ_METHODS = frozenset(('eth_chainId', 'eth_getBlockByNumber', 'eth_getTransactionReceipt',
                         'getGenesisHash', 'getSlot', 'getBlock', 'server_info', 'ledger'))


def read_http(url, body=None, text=False, headers=None):
    """No raw response / credential URLs in exceptions or logs."""
    if urllib.parse.urlsplit(url).scheme != 'https':
        raise ValueError('HTTPS_REQUIRED')
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
        headers={'Accept': 'application/json,text/plain', 'Content-Type': 'application/json',
                 'User-Agent': 'BORU-ReadOnly/3.1', **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            raw = response.read(12_000_001)
    except urllib.error.HTTPError as exc:
        raise ValueError('HTTP_' + str(exc.code)) from None
    except (OSError, urllib.error.URLError):
        raise ValueError('NETWORK_UNAVAILABLE') from None
    if len(raw) > 12_000_000:
        raise ValueError('RESPONSE_TOO_LARGE')
    try:
        return raw.decode().strip() if text else json.loads(raw)
    except (UnicodeError, ValueError):
        raise ValueError('INVALID_RESPONSE') from None


def transfer(chain, txid, index, quantity, sender=None, recipient=None, kind='native_transfer'):
    return {'event_id': f'{chain}:{txid}:{index}', 'tx_hash': txid, 'kind': kind,
            'quantity': quantity, 'asset': ASSETS[chain], 'sender': sender,
            'recipient': recipient, 'side': 'UNKNOWN', 'owner': 'UNKNOWN',
            'owner_verified': False, 'company_ownership': 'NOT_VERIFIED'}


def parse_bitcoin(rows, block_hash):
    events = []
    for tx in rows:
        if any(v.get('is_coinbase') for v in tx.get('vin', [])):
            continue
        if not tx.get('status', {}).get('confirmed') or tx['status'].get('block_hash') != block_hash:
            raise ValueError('UNCONFIRMED_BITCOIN_TRANSACTION')
        for i, out in enumerate(tx['vout']):
            value = int(out['value']) / 1e8
            if value >= THRESHOLDS['bitcoin']:
                event = transfer('bitcoin', tx['txid'], i, value,
                    recipient=out.get('scriptpubkey_address'), kind='utxo_output')
                event['may_be_change_or_self_transfer'] = True
                events.append(event)
    return events


def parse_solana(rows):
    events = []
    for tx in rows:
        meta = tx.get('meta')
        if not isinstance(meta, dict) or meta.get('err') is not None:
            continue
        tid = tx['transaction']['signatures'][0]
        instructions = list(tx['transaction']['message'].get('instructions', []))
        for inner in meta.get('innerInstructions') or []:
            instructions.extend(inner.get('instructions', []))
        for index, instruction in enumerate(instructions):
            if instruction.get('programId') != '11111111111111111111111111111111':
                continue
            parsed = instruction.get('parsed', {})
            if parsed.get('type') not in ('transfer', 'transferWithSeed'):
                continue
            info = parsed['info']
            quantity = int(info['lamports']) / 1e9
            if quantity >= THRESHOLDS['solana']:
                events.append(transfer('solana', tid, index, quantity, info.get('source'), info.get('destination')))
    return events


def parse_tron(rows):
    events = []
    for tx in rows:
        results = tx.get('ret', [])
        if not results or any(r.get('contractRet') != 'SUCCESS' for r in results):
            continue
        for i, contract in enumerate(tx.get('raw_data', {}).get('contract', [])):
            if contract.get('type') != 'TransferContract':
                continue
            value = contract['parameter']['value']
            quantity = int(value['amount']) / 1e6
            if quantity >= THRESHOLDS['tron']:
                events.append(transfer('tron', tx['txID'], i, quantity,
                                       value.get('owner_address'), value.get('to_address')))
    return events


def parse_xrpl(rows):
    events = []
    for row in rows:
        tx = row.get('tx_json', row.get('tx', row))
        meta = row.get('metaData', row.get('meta', {}))
        if tx.get('TransactionType') != 'Payment' or meta.get('TransactionResult') != 'tesSUCCESS':
            continue
        # Amount/DeliverMax is NOT the amount received on partial payments.
        delivered = meta.get('delivered_amount')
        if not isinstance(delivered, str) or not delivered.isdigit():
            continue
        quantity = int(delivered) / 1e6
        if quantity >= THRESHOLDS['xrpl']:
            txid = row.get('hash', tx.get('hash'))
            if not txid:
                raise ValueError('MISSING_TRANSACTION_ID')
            events.append(transfer('xrpl', txid, 0, quantity, tx.get('Account'), tx.get('Destination')))
    return events


class ChainReader:
    def __init__(self, transport=read_http, clock=time.time):
        self.transport, self.clock = transport, clock
        self.endpoints = {c: os.environ.get('BORU_' + c.upper() + '_URL', DEFAULTS[c]).rstrip('/') for c in CHAINS}
        self.verified = set()

    def request(self, chain, path='', body=None, text=False):
        headers = {}
        if chain == 'tron' and os.environ.get('TRONGRID_API_KEY'):
            headers['TRON-PRO-API-KEY'] = os.environ['TRONGRID_API_KEY']
        return self.transport(self.endpoints[chain] + path, body, text=text, headers=headers)

    def rpc(self, chain, method, params):
        if method not in READ_METHODS:
            raise ValueError('READ_ONLY_METHOD_REQUIRED')
        data = self.request(chain, '/jsonrpc' if chain == 'tron' else '',
                            {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
        result = data.get('result')
        if data.get('error') or result is None or isinstance(result, dict) and result.get('error'):
            raise ValueError('RPC_ERROR')
        return result

    def verify(self, chain):
        if chain in self.verified:
            return
        if chain in ('ethereum', 'bnb', 'tron'):
            expected = {'ethereum': 1, 'bnb': 56, 'tron': 728126428}[chain]
            correct = int(self.rpc(chain, 'eth_chainId', []), 16) == expected
        elif chain == 'bitcoin':
            correct = self.request(chain, '/block-height/0', text=True) == BTC_GENESIS
        elif chain == 'solana':
            correct = self.rpc(chain, 'getGenesisHash', []) == SOL_GENESIS
        else:
            info = self.rpc(chain, 'server_info', [{}])['info']
            correct = info.get('network_id') == 0
        if not correct:
            raise ValueError('MAINNET_IDENTITY_UNCONFIRMED')
        self.verified.add(chain)

    def evm(self, chain):
        block = self.rpc(chain, 'eth_getBlockByNumber', ['finalized', True])
        events, checked = [], 0
        for tx in block['transactions']:
            value = int(tx['value'], 16) / 1e18
            if value < THRESHOLDS[chain]:
                continue
            if checked >= 50:
                break
            checked += 1
            receipt = self.rpc(chain, 'eth_getTransactionReceipt', [tx['hash']])
            if receipt.get('blockHash') != block['hash']:
                raise ValueError('REORG_OR_INCONSISTENT_RECEIPT')
            if int(receipt.get('status', '0x0'), 16) == 1:
                events.append(transfer(chain, tx['hash'], 0, value, tx.get('from'), tx.get('to'),
                                       'successful_top_level_native_value'))
        candidates = sum(int(tx['value'], 16) / 1e18 >= THRESHOLDS[chain] for tx in block['transactions'])
        return {'height': int(block['number'], 16), 'block_hash': block['hash'],
                'block_time': int(block['timestamp'], 16), 'finality': 'finalized',
                'sampled_transactions': len(block['transactions']), 'events': events,
                'receipt_checks': checked, 'receipt_limit_reached': candidates > checked,
                'scope': 'one finalized block; top-level native value only; internal/token transfers excluded'}

    def bitcoin(self):
        head = int(self.request('bitcoin', '/blocks/tip/height', text=True))
        height = head - 5
        block_hash = self.request('bitcoin', '/block-height/' + str(height), text=True)
        block = self.request('bitcoin', '/block/' + block_hash)
        if block['id'] != block_hash or block['height'] != height:
            raise ValueError('INCONSISTENT_BITCOIN_BLOCK')
        rows = self.request('bitcoin', '/block/' + block_hash + '/txs')
        if self.request('bitcoin', '/block-height/' + str(height), text=True) != block_hash:
            raise ValueError('BITCOIN_REORG')
        return {'height': height, 'block_hash': block_hash, 'block_time': block['timestamp'],
                'finality': 'six_confirmations_not_absolute_finality', 'sampled_transactions': len(rows),
                'total_block_transactions': block['tx_count'], 'events': parse_bitcoin(rows, block_hash),
                'scope': 'first 25 transactions of one six-confirmation block; outputs can include change'}

    def solana(self):
        slot = self.rpc('solana', 'getSlot', [{'commitment': 'finalized'}])
        block = self.rpc('solana', 'getBlock', [slot, {'commitment': 'finalized', 'encoding': 'jsonParsed',
             'transactionDetails': 'full', 'rewards': False, 'maxSupportedTransactionVersion': 1}])
        return {'height': slot, 'block_hash': block['blockhash'], 'block_time': block['blockTime'],
                'finality': 'finalized', 'sampled_transactions': len(block['transactions']),
                'events': parse_solana(block['transactions']),
                'scope': 'one finalized slot; successful parsed System Program transfers; SPL swaps excluded'}

    def tron(self):
        block = self.request('tron', '/walletsolidity/getnowblock', {})
        header = block['block_header']['raw_data']
        rows = block.get('transactions', [])
        return {'height': header['number'], 'block_hash': block['blockID'], 'block_time': header['timestamp'] / 1000,
                'finality': 'solidified', 'sampled_transactions': len(rows), 'events': parse_tron(rows),
                'scope': 'one solidified block; successful native TRX transfers; TRC20 and DEX decoding excluded'}

    def xrpl(self):
        result = self.rpc('xrpl', 'ledger', [{'ledger_index': 'validated', 'transactions': True, 'expand': True}])
        if result.get('validated') is not True:
            raise ValueError('XRPL_LEDGER_NOT_VALIDATED')
        ledger = result['ledger']
        rows = ledger.get('transactions', [])
        return {'height': int(ledger['ledger_index']), 'block_hash': ledger['ledger_hash'],
                'block_time': int(ledger['close_time']) + 946684800, 'finality': 'validated',
                'sampled_transactions': len(rows), 'events': parse_xrpl(rows),
                'scope': 'one validated ledger; delivered native XRP payments only; DEX offer decoding excluded'}

    def snapshot(self, chain):
        if chain not in CHAINS:
            raise ValueError('UNSUPPORTED_CHAIN')
        now = self.clock()
        base = {'chain': chain, 'asset': ASSETS[chain], 'received': now,
                'native_threshold': THRESHOLDS[chain], 'trade_direction_supported': False,
                'company_ownership': 'NOT_VERIFIED', 'continuous_history_complete': False,
                'endpoint_id': hashlib.sha256(self.endpoints[chain].encode()).hexdigest()[:16]}
        try:
            self.verify(chain)
            result = self.evm(chain) if chain in ('ethereum', 'bnb') else getattr(self, chain)()
            if not isinstance(result['block_time'], (int, float)):
                raise ValueError('MISSING_BLOCK_TIME')
            max_age = 14400 if chain == 'bitcoin' else 1800 if chain == 'ethereum' else 300
            if not -5 <= self.clock() - result['block_time'] <= max_age:
                raise ValueError('STALE_OR_FUTURE_BLOCK')
            if len({e['event_id'] for e in result['events']}) != len(result['events']):
                raise ValueError('DUPLICATE_CHAIN_EVENTS')
            return {**base, **result, 'status': 'READY', 'received': self.clock(),
                    'network_verified': True, 'event_count': len(result['events'])}
        except Exception as exc:
            # Error text is controlled locally; never leak provider credentials.
            reason = str(exc) if isinstance(exc, ValueError) and str(exc).replace('_', '').isalnum() else type(exc).__name__
            return {**base, 'status': 'DATA_ERROR', 'error': reason, 'network_verified': chain in self.verified}


class ChainMonitor:
    """Independent bounded pool; sampling never blocks trade/risk loops."""
    def __init__(self, reader=None):
        self.reader = reader or ChainReader()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.pending, self.attempted, self.data = {}, {}, {}

    def step(self, now):
        updates = []
        for chain, future in list(self.pending.items()):
            if not future.done():
                continue
            result = future.result()
            previous = self.data.get(chain, {})
            if result.get('status') == 'READY' and previous.get('status') == 'READY':
                result['previous_sample_height'] = previous['height']
                result['unscanned_height_interval'] = max(0, result['height'] - previous['height'] - 1)
                if result['height'] < previous['height'] or (result['height'] == previous['height'] and result['block_hash'] != previous['block_hash']):
                    result['status'] = 'DATA_ERROR'
                    result['error'] = 'CHAIN_ROLLBACK_DETECTED'
            self.data[chain] = result
            del self.pending[chain]
            updates.append(('__chain_activity__:' + chain, result))
        for chain in sorted(CHAINS, key=lambda c: self.attempted.get(c, 0)):
            if len(self.pending) >= 2:
                break
            if chain in self.pending or now - self.attempted.get(chain, 0) < 60:
                continue
            self.attempted[chain] = now
            self.pending[chain] = self.pool.submit(self.reader.snapshot, chain)
        return updates

    def context(self, asset, now):
        chain = next((c for c in CHAINS if ASSETS[c] == asset), None)
        result = dict(self.data.get(chain, {'status': 'PENDING' if chain else 'UNSUPPORTED_ASSET'}))
        result['fresh'] = result.get('status') == 'READY' and 0 <= now - result.get('received', 0) <= 180
        return result

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser(description='Read-only six-mainnet snapshot; no simulation or orders.')
    parser.add_argument('--output', default='chain-data')
    parser.add_argument('--require-all', action='store_true')
    args = parser.parse_args()
    directory = Path(args.output); directory.mkdir(parents=True, exist_ok=True)
    reader = ChainReader()
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = dict(zip(CHAINS, pool.map(reader.snapshot, CHAINS)))
    payload = {'mode': 'READ_ONLY_CHAIN_SAMPLE', 'created_utc': time.time(), 'chains': results}
    (directory / 'six-chains.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    for name, result in results.items():
        print(json.dumps({k: result.get(k) for k in ('chain', 'status', 'height', 'finality', 'event_count', 'error')}, ensure_ascii=False))
    if args.require_all and any(r['status'] != 'READY' for r in results.values()):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
