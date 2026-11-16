"""Turkish per-fill reports derived solely from the saved paper-trading ledger."""
import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Istanbul')
FEE = .0035


def money(v):
    return f'{v:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.') + ' TL'


def local(ts):
    return datetime.fromtimestamp(ts, TZ)


def atomic_json(path, content):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def records(db):
    """Replay cost basis and cash exactly, including partial sells."""
    db.row_factory = sqlite3.Row
    cash, realized, holdings, result = 20000., 0., {}, []
    for row in db.execute('SELECT * FROM trades ORDER BY id'):
        t = dict(row)
        m, qty, gross = t['market'], t['qty'], t['qty']*t['price']
        if t['side'] == 'BUY':
            if m in holdings:
                raise ValueError('Unexpected overlapping position')
            cost = gross+t['fee']
            cash -= cost
            holdings[m] = dict(qty=qty, cost=cost, entry_ts=t['ts'], entry_price=t['price'])
            unit_cost, entry_ts, entry_price, return_pct = cost/qty, t['ts'], t['price'], None
            signal = json.loads(t['reason'])
            reason = ('EMA10 > EMA30; 20 dakikalık zirve kırılımı; RSI ve oynaklık filtreleri; pozitif momentum.')
        else:
            p = holdings[m]
            if qty > p['qty']+1e-9:
                raise ValueError('Sale exceeds recorded position')
            cost, unit_cost = p['cost']*qty/p['qty'], p['cost']/p['qty']
            entry_ts, entry_price = p['entry_ts'], p['entry_price']
            net = gross-t['fee']
            if not math.isclose(t['pnl'], net-cost, abs_tol=.00001):
                raise ValueError('Ledger profit does not match cost basis')
            return_pct = (net/cost-1)*100
            cash += net
            realized += t['pnl']
            p['qty'], p['cost'] = p['qty']-qty, p['cost']-cost
            if p['qty'] < 1e-10:
                del holdings[m]
            signal = {}
            reason = 'Net zarar kes eşiği tetiklendi.' if t['reason']=='STOP' else 'Net kâr al eşiği tetiklendi.'
        t.update(timestamp_local=local(t['ts']).isoformat(), cash_after_tl=cash,
                 realized_total_tl=realized, allocated_cost_tl=cost, net_return_pct=return_pct,
                 notional_tl=gross, reason_tr=reason, indicators=signal,
                 entry_ts=entry_ts, entry_price=entry_price,
                 stop_reference_price=unit_cost*.98/(1-FEE),
                 target_reference_price=unit_cost*1.03/(1-FEE),
                 remaining_qty=holdings.get(m, {}).get('qty', 0),
                 mode='PAPER_ONLY', assumed_fee_each_side=FEE)
        result.append(t)
    return result


def chart(db, trade, history, path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':11,'axes.spines.top':False,
                         'axes.spines.right':False,'figure.facecolor':'#f4f7fb','axes.facecolor':'white'})
    ts, market = trade['ts'], trade['market']
    start = min(ts-3600, trade['entry_ts']-600)
    ticks = db.execute('SELECT timestamp,bid,ask FROM ticks WHERE market=? AND timestamp>=? AND timestamp<=? ORDER BY timestamp',
                       (market,start,ts)).fetchall()
    fig = plt.figure(figsize=(12,10), layout='constrained')
    grid = fig.add_gridspec(4,1,height_ratios=[.7,2.6,1.3,.95])
    head = fig.add_subplot(grid[0]); head.axis('off')
    side = 'ALIŞ' if trade['side']=='BUY' else 'SATIŞ'
    head.text(0, .85, f'BÖRÜ  /  {market.upper().replace("_", "/")}  /  {side}', fontsize=22, fontweight='bold',color='#17304c')
    head.text(0,.38,f'SANAL İŞLEM #{trade["id"]}   •   {local(ts):%d.%m.%Y %H:%M:%S} Türkiye saati',fontsize=12,color='#506279')
    ax = fig.add_subplot(grid[1])
    if ticks:
        times = [local(r[0]) for r in ticks]
        ax.plot(times,[(r[1]+r[2])/2 for r in ticks],color='#2558a1',lw=1.6,label='Gözlenen alış/satış orta fiyatı')
        ax.fill_between(times,[r[1] for r in ticks],[r[2] for r in ticks],color='#2558a1',alpha=.12,label='Alış–satış farkı')
    else:
        ax.text(.02,.92,'Bu aralıkta kaydedilmiş fiyat serisi yok.',transform=ax.transAxes)
    ax.axhline(trade['stop_reference_price'],color='#c23d4b',ls='--',lw=1,label='−%2 net zarar kes referansı')
    ax.axhline(trade['target_reference_price'],color='#16816b',ls='--',lw=1,label='+%3 net kâr al referansı')
    if trade['side']=='SELL':
        ax.scatter([local(trade['entry_ts'])],[trade['entry_price']],marker='^',s=95,color='#16816b',zorder=5,label='Sanal alış')
    ax.scatter([local(ts)],[trade['price']],marker='^' if side=='ALIŞ' else 'v',s=110,
               color='#16816b' if side=='ALIŞ' else '#c23d4b',zorder=6,label='Bu sanal işlem')
    ax.set_ylabel('Fiyat (TL)'); ax.grid(alpha=.16)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M',tz=TZ))
    ax.legend(loc='best',fontsize=9)
    balance = fig.add_subplot(grid[2])
    prior = [t for t in history if t['id']<=trade['id']]
    balance.step([local(t['ts']) for t in prior],[t['realized_total_tl'] for t in prior],where='post',color='#743db3',lw=2,marker='o',markersize=4)
    balance.set_xlim(local(prior[0]['ts']-30),local(max(ts,prior[0]['ts']+30)+30))
    balance.axhline(0,color='#8997a7',lw=.8)
    balance.set_title('Birikimli gerçekleşmiş kâr / zarar',loc='left',fontsize=12)
    balance.set_ylabel('TL'); balance.grid(alpha=.16)
    balance.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M',tz=TZ))
    foot = fig.add_subplot(grid[3]); foot.axis('off')
    pnl = 'Pozisyon açıldı; satış sonucu henüz yok.' if side=='ALIŞ' else f'Bu satış: {money(trade["pnl"])}  /  %{trade["net_return_pct"]:.2f}'
    text = (f'Gerçekleşme: {trade["price"]:.8g} TL × {trade["qty"]:.8g}    |    Komisyon: {money(trade["fee"])}\n'
            f'{pnl}\nKalan nakit: {money(trade["cash_after_tl"])}    |    Birikimli gerçekleşmiş sonuç: {money(trade["realized_total_tl"])}\n'
            'Her yön için %0,35 komisyon varsayımı. Grafik işlem bazlı mum değil, kaydedilmiş fiyat gözlemleridir.\n'
            'Kesikli seviyeler net getiri referansıdır; derinlik ve gecikme nedeniyle gerçekleşme değişebilir.')
    foot.text(0,.96,text,va='top',fontsize=10,linespacing=1.65,color='#33465b')
    fig.savefig(path,dpi=150)
    plt.close(fig)


def generate(directory, output=None):
    directory = Path(directory)
    output = Path(output) if output else directory/'trade_reports'
    output.mkdir(parents=True,exist_ok=True)
    db = sqlite3.connect(directory/'simulation.sqlite3')
    started = db.execute("SELECT value FROM settings WHERE key='started'").fetchone()[0]
    session = hashlib.sha256(started.encode()).hexdigest()[:16]
    all_trades = records(db)
    index = {'mode':'PAPER_ONLY','session_id':session,'started_utc':started,'trades':[]}
    for trade in all_trades:
        ident = f'{session}-{trade["id"]:06d}'
        trade['report_id'] = ident
        stem = output/ident
        paths = {suffix:stem.with_suffix('.'+suffix) for suffix in ('png','json','md')}
        if not all(p.exists() for p in paths.values()):
            chart(db,trade,all_trades,paths['png'])
            atomic_json(paths['json'],trade)
            side = 'ALIŞ' if trade['side']=='BUY' else 'SATIŞ'
            pnl = 'Henüz satış gerçekleşmedi.' if side=='ALIŞ' else f'{money(trade["pnl"])} (%{trade["net_return_pct"]:.2f})'
            text = (f'# BÖRÜ — {market_label(trade)} {side}\n\n'
                    f'**Sanal işlem.** {trade["timestamp_local"]}\n\n'
                    f'![İşlem grafiği]({paths["png"].name})\n\n'
                    f'| Alan | Değer |\n|---|---|\n| İşlem kimliği | {ident} |\n'
                    f'| Fiyat | {trade["price"]:.8g} TL |\n| Miktar | {trade["qty"]:.8g} |\n'
                    f'| İşlem tutarı | {money(trade["notional_tl"])} |\n| Komisyon | {money(trade["fee"])} |\n'
                    f'| Bu satışın net sonucu | {pnl} |\n| Kalan nakit | {money(trade["cash_after_tl"])} |\n'
                    f'| Birikimli gerçekleşmiş sonuç | {money(trade["realized_total_tl"])} |\n'
                    f'| Üründe kalan miktar | {trade["remaining_qty"]:.8g} |\n\n'
                    f'Gerekçe: {trade["reason_tr"]}\n\n'
                    f'Gösterge değerleri: {json.dumps(trade["indicators"],ensure_ascii=False)}\n\n'
                    'Komisyon her yön için %0,35 varsayılmıştır. Kalan nakit, toplam portföy değeri değildir. '
                    'Grafik kayıtlı alış/satış gözlemlerini kullanır; eksik geçmiş veya işlem sonrası fiyat eklenmez.\n')
            paths['md'].write_text(text,encoding='utf-8')
        index['trades'].append({'report_id':ident,'trade_id':trade['id'],'market':trade['market'],
                               'side':trade['side'],'timestamp_local':trade['timestamp_local'],
                               **{k:p.name for k,p in paths.items()}})
    atomic_json(output/'index.json',index)
    db.close()
    return index


def market_label(t):
    return t['market'].upper().replace('_','/')


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--directory',default='data')
    p.add_argument('--output')
    a=p.parse_args()
    index=generate(a.directory,a.output)
    print(json.dumps({'report_count':len(index['trades']),'session_id':index['session_id']}),flush=True)
