"""Three logical bots / four Grok roles. Read-only review; no order or ledger writer.

The running v3 risk/execution engine remains independent. Live Grok activation needs
the owner's XAI_API_KEY; a missing key never produces pretend agent output.
"""
import argparse
import concurrent.futures
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
ROLES = ('technical', 'chain', 'decision', 'report')
BOTS = {'BORU_ANALIZ': ['technical', 'chain'], 'BORU_KARAR': ['decision'], 'BORU_RAPOR': ['report']}
API_URL = 'https://api.x.ai/v1/responses'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def schema(role):
    properties = {
        'packet_id': {'type': 'string'}, 'market': {'type': 'string'},
        'evidence_ids': {'type': 'array', 'items': {'type': 'string'}},
        'reason_tr': {'type': 'string'}, 'limitations_tr': {'type': 'array', 'items': {'type': 'string'}},
    }
    if role == 'decision':
        properties.update(action={'type': 'string', 'enum': ['BUY', 'SELL', 'HOLD']},
                          budget_fraction={'type': 'number', 'minimum': 0, 'maximum': 1})
    elif role == 'report':
        properties['heading'] = {'type': 'string', 'enum': ['SANAL İŞLEM']}
    else:
        properties['assessment'] = {'type': 'string', 'enum': ['SUPPORT', 'OPPOSE', 'MIXED', 'INSUFFICIENT_DATA']}
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


def validate_result(role, result, packet):
    fields = schema(role)['properties']
    if not isinstance(result, dict) or set(result) != set(fields):
        raise ValueError('AGENT_SCHEMA_MISMATCH')
    if result['packet_id'] != packet['packet_id'] or result['market'] != packet['market']:
        raise ValueError('AGENT_PACKET_MISMATCH')
    if not isinstance(result['reason_tr'], str) or not 1 <= len(result['reason_tr']) <= 4000:
        raise ValueError('INVALID_REASON')
    ids = result['evidence_ids']
    known = {e['evidence_id'] for e in packet['evidence']}
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in known for i in ids):
        raise ValueError('UNKNOWN_OR_MISSING_EVIDENCE')
    if not isinstance(result['limitations_tr'], list) or any(not isinstance(x, str) for x in result['limitations_tr']):
        raise ValueError('INVALID_LIMITATIONS')
    for name, definition in fields.items():
        if 'enum' in definition and result[name] not in definition['enum']:
            raise ValueError('INVALID_AGENT_ENUM')
    if role == 'decision':
        fraction = result['budget_fraction']
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not math.isfinite(fraction) or not 0 <= fraction <= 1:
            raise ValueError('INVALID_BUDGET_FRACTION')
        if result['action'] != 'BUY' and fraction != 0:
            raise ValueError('BUDGET_ONLY_FOR_BUY')
    return result


class GrokClient:
    def __init__(self, api_key=None, model=None):
        self.api_key = api_key if api_key is not None else os.environ.get('XAI_API_KEY')
        self.model = model or os.environ.get('BORU_GROK_MODEL', 'grok-4.7')
        if not self.api_key:
            raise ValueError('GROK_ACCOUNT_CONNECTION_REQUIRED')

    def review(self, role, packet, analyses=None):
        if role not in ROLES:
            raise ValueError('UNKNOWN_AGENT_ROLE')
        common = (ROOT / 'grok' / 'common.txt').read_text()
        prompt = (ROOT / 'grok' / (role + '.txt')).read_text()
        payload = {'model': self.model, 'store': False, 'max_output_tokens': 2400,
            'input': [{'role': 'system', 'content': common + '\n' + prompt},
                      {'role': 'user', 'content': canonical({'data_packet': packet, 'analyses': analyses or {}})}],
            'text': {'format': {'type': 'json_schema', 'name': 'boru_' + role,
                               'schema': schema(role), 'strict': True}}}
        # No search, X, code execution, remote tools, or exchange authentication.
        request = urllib.request.Request(API_URL, data=canonical(payload).encode(),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.api_key})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                raw = response.read(1_000_001)
        except urllib.error.HTTPError as exc:
            raise ValueError('GROK_HTTP_' + str(exc.code)) from None
        except (OSError, urllib.error.URLError):
            raise ValueError('GROK_NETWORK_UNAVAILABLE') from None
        if len(raw) > 1_000_000:
            raise ValueError('GROK_RESPONSE_TOO_LARGE')
        result = json.loads(raw)
        if result.get('status') != 'completed':
            raise ValueError('GROK_RESPONSE_INCOMPLETE')
        chunks = [c['text'] for item in result.get('output', []) if item.get('type') == 'message'
                  for c in item.get('content', []) if c.get('type') == 'output_text']
        return validate_result(role, json.loads(''.join(chunks)), packet)


def load_packet(directory, market, now=None):
    now = time.time() if now is None else now
    directory = Path(directory).resolve()
    db_path = directory / 'simulation.sqlite3'
    if not db_path.is_file():
        raise ValueError('EXISTING_LEDGER_REQUIRED')
    db = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')  # One consistent read snapshot, never mutate the account.
        row = db.execute('SELECT ts,payload FROM research_snapshots WHERE market=? ORDER BY ts DESC LIMIT 1', (market,)).fetchone()
        if row is None:
            raise ValueError('NO_RESEARCH_FOR_MARKET')
        research = json.loads(row['payload'])
        evidence = [{'evidence_id': digest(research), 'kind': 'market_research', 'observed_at': row['ts'], 'data': research}]
        chain = {'btc_tl': 'bitcoin', 'eth_tl': 'ethereum', 'sol_tl': 'solana',
                 'bnb_tl': 'bnb', 'trx_tl': 'tron', 'xrp_tl': 'xrpl'}.get(market)
        if chain:
            activity = db.execute('SELECT ts,payload FROM research_snapshots WHERE market=? ORDER BY ts DESC LIMIT 1',
                                  ('__chain_activity__:' + chain,)).fetchone()
            if activity:
                data = json.loads(activity['payload'])
                evidence.append({'evidence_id': digest(data), 'kind': 'native_chain_sample',
                                 'observed_at': activity['ts'], 'data': data})
        portfolio = {'account': [dict(r) for r in db.execute('SELECT * FROM account')],
                     'positions': [dict(r) for r in db.execute('SELECT * FROM positions')]}
        packet = {'market': market, 'created_at': now, 'valid_until': now + 120,
                  'mode': 'PAPER_REVIEW_ONLY', 'evidence': evidence, 'portfolio': portfolio,
                  'research_fresh': research.get('status') == 'READY' and 0 <= now - research.get('received', 0) <= 360,
                  'can_execute': False, 'source': 'existing_session_sqlite_read_only'}
        packet['packet_id'] = digest(packet)
        return packet
    finally:
        db.close()


def risk_review(proposal, packet, now=None):
    """A typed review outcome, never an instruction to execute or bypass v3 risk."""
    now = time.time() if now is None else now
    validate_result('decision', proposal, packet)
    reasons = []
    if now > packet['valid_until'] or now < packet['created_at']:
        reasons.append('EXPIRED_OR_FUTURE_PACKET')
    if proposal['action'] == 'BUY' and not packet.get('research_fresh'):
        reasons.append('STALE_RESEARCH_NO_NEW_RISK')
    if proposal['action'] == 'SELL' and not any(p.get('market') == packet['market'] for p in packet['portfolio']['positions']):
        reasons.append('NO_POSITION_TO_SELL')
    return {'proposal_usable_for_review': not reasons, 'rejections': reasons, 'can_execute': False,
            'execution_blocker': 'ADVISORY_MODE_REQUIRES_SEPARATE_VALIDATED_INTEGRATION'}


class GrokTeam:
    def __init__(self, client=None):
        self.client = client or GrokClient()

    def analyze(self, packet):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = {role: executor.submit(self.client.review, role, packet) for role in ('technical', 'chain')}
            analyses = {role: validate_result(role, future.result(), packet) for role, future in futures.items()}
        proposal = validate_result('decision', self.client.review('decision', packet, analyses), packet)
        return {'mode': 'ADVISORY_ONLY', 'packet': packet, 'analyses': analyses,
                'proposal': proposal, 'risk_review': risk_review(proposal, packet), 'model': self.client.model}

    def explain_report(self, packet):
        if packet.get('mode') != 'VERIFIED_PAPER_REPORT':
            raise ValueError('VERIFIED_REPORT_PACKET_REQUIRED')
        return validate_result('report', self.client.review('report', packet), packet)


def main():
    parser = argparse.ArgumentParser(description='BÖRÜ Grok team: review only, never submit orders.')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--directory')
    parser.add_argument('--market')
    parser.add_argument('--output', default='grok-review')
    args = parser.parse_args()
    if args.check:
        print(json.dumps({'bots': BOTS, 'agents': len(ROLES), 'api_key_present': bool(os.environ.get('XAI_API_KEY')),
                          'model': os.environ.get('BORU_GROK_MODEL', 'grok-4.7'),
                          'mode': 'ADVISORY_ONLY', 'native_grok_bot_profiles_created': False}, ensure_ascii=False))
        return
    if not args.directory or not args.market:
        parser.error('--directory and --market are required for a review')
    try:
        team = GrokTeam()  # Fail before pretending to call agents if no account connection.
        packet = load_packet(args.directory, args.market)
        result = team.analyze(packet)
        out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
        target = out / (packet['packet_id'] + '.json')
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(str(target))
    except ValueError as exc:
        print(json.dumps({'status': 'BLOCKED', 'reason': str(exc), 'can_execute': False}))
        raise SystemExit(2) from None


if __name__ == '__main__':
    main()
