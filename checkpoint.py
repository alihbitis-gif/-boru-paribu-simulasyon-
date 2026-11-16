"""Create a consistent, resumable snapshot without stopping the paper worker."""
import argparse
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone


def checkpoint(directory, output):
    directory, output = Path(directory), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect((directory/'simulation.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)
    target = sqlite3.connect(output/'simulation.sqlite3')
    try:
        source.backup(target)
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Checkpoint integrity check failed')
        started = target.execute("SELECT value FROM settings WHERE key='started'").fetchone()[0]
        count = target.execute('SELECT COUNT(*) FROM trades').fetchone()[0]
        (output/'checkpoint.json').write_text(json.dumps({
            'mode':'PAPER_ONLY','started_utc':started,'trade_events':count,
            'checkpoint_utc':datetime.now(timezone.utc).isoformat(),
            'purpose':'Restore this SQLite ledger in a single v2 worker; do not reset cash.'
        },indent=2),encoding='utf-8')
    finally:
        target.close();source.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',default='data')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    checkpoint(args.directory,args.output)
