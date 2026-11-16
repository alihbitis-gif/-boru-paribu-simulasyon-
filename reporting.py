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
LEGACY_FEE = .0035


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
    has_fills = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fills'").fetchone()
    for row in db.execute('SELECT * FROM trades ORDER BY id'):
        t = dict(row)
        has_context=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_context'").fetchone()
        audit=db.execute('SELECT version,payload FROM execution_context WHERE trade_id=?',(t['id'],)).fetchone() if has_context else None
        version=audit['version'] if audit else '2.0' if has_fills else '1.0'
        audit_data=json.loads(audit['payload']) if audit else {}
        fill = db.execute('SELECT * FROM fills WHERE trade_id=?',(t['id'],)).fetchone() if has_fills else None
        rate = fill['fee_rate'] if fill else LEGACY_FEE
        m, qty = t['market'], t['qty']
        gross = fill['notional'] if fill else qty*t['price']
        gross_qty = fill['gross_qty'] if fill else qty
        fee_currency = fill['fee_currency'] if fill else 'TL'
        fee_quantity = fill['fee_quantity'] if fill else t['fee']
        if t['side'] == 'BUY':
            if m in holdings:
                raise ValueError('Unexpected overlapping position')
            cost = gross if fill else gross+t['fee']
            cash -= cost
            holdings[m] = dict(qty=qty, cost=cost, entry_ts=t['ts'], entry_price=t['price'])
            unit_cost, entry_ts, entry_price, return_pct = cost/qty, t['ts'], t['price'], None
            signal = json.loads(t['reason'])
            reason = ('Alış fiyatı tarafında tamponlu kırılım; değişen son işlem fiyatıyla teyit; '
                      '5 dakikalık eğilim, RSI ve oynaklık filtreleri; risk ve maliyet bütçesi.' if fill else
                      'EMA10 > EMA30; 20 dakikalık orta fiyat zirve kırılımı; RSI ve oynaklık filtreleri; pozitif momentum.')
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
        if version=='3.0':
            from strategy import REASONS_TR
            decision=audit_data.get('decision',t['reason'])
            reason=REASONS_TR.get(decision,'Veriye dayalı sanal karar: '+str(decision))
        t.update(timestamp_local=local(t['ts']).isoformat(), cash_after_tl=cash,
                 realized_total_tl=realized, allocated_cost_tl=cost, net_return_pct=return_pct,
                 notional_tl=gross, reason_tr=reason, indicators=signal,
                 entry_ts=entry_ts, entry_price=entry_price,
                 stop_reference_price=unit_cost*.98/(1-rate),
                 target_reference_price=unit_cost*1.03/(1-rate),
                 remaining_qty=holdings.get(m, {}).get('qty', 0),
                 mode='PAPER_ONLY', assumed_fee_each_side=rate,
                 fee_currency=fee_currency,fee_quantity=fee_quantity,gross_quantity=gross_qty,
                 net_acquired_quantity=qty if t['side']=='BUY' else None,
                 strategy_version=version,decision_context=audit_data,
                 threshold_mode='REVIEW_POINTS' if version=='3.0' else 'FIXED_EXITS',
                 hard_stop_net_return=audit_data.get('config',{}).get('hard_stop',-.05) if version=='3.0' else None,
                 emergency_exit_reference=unit_cost*(1+audit_data.get('config',{}).get('hard_stop',-.05))/(1-rate) if version=='3.0' else None,
                 data_source='sampled_public_quotes_and_last_price' if fill else 'sampled_public_quotes')
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
    is_v3=trade['strategy_version']=='3.0'
    fig = plt.figure(figsize=(12,13 if is_v3 else 10), layout='constrained')
    grid = fig.add_gridspec(5 if is_v3 else 4,1,height_ratios=[.7,2.6,2.1,1.3,1.1] if is_v3 else [.7,2.6,1.3,.95])
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
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ticker_observations'").fetchone():
        observed = db.execute('SELECT received,last FROM ticker_observations WHERE market=? AND received>=? AND received<=? ORDER BY received',(market,start,ts)).fetchall()
        if observed:
            ax.plot([local(r[0]) for r in observed],[r[1] for r in observed],color='#a46716',lw=1,label='Örneklenen son işlem fiyatı')
    ax.axhline(trade['stop_reference_price'],color='#c23d4b',ls='--',lw=1,label='−%2 karar inceleme referansı' if is_v3 else '−%2 net zarar kes referansı')
    ax.axhline(trade['target_reference_price'],color='#16816b',ls='--',lw=1,label='+%3 karar inceleme referansı' if is_v3 else '+%3 net kâr al referansı')
    if trade['side']=='SELL':
        ax.scatter([local(trade['entry_ts'])],[trade['entry_price']],marker='^',s=95,color='#16816b',zorder=5,label='Sanal alış')
    ax.scatter([local(ts)],[trade['price']],marker='^' if side=='ALIŞ' else 'v',s=110,
               color='#16816b' if side=='ALIŞ' else '#c23d4b',zorder=6,label='Bu sanal işlem')
    ax.set_ylabel('Fiyat (TL)'); ax.grid(alpha=.16)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M',tz=TZ))
    ax.legend(loc='best',fontsize=9)
    if is_v3:
        from matplotlib.patches import Rectangle
        candle_ax=fig.add_subplot(grid[2])
        rows=db.execute("SELECT payload FROM candles WHERE market=? AND interval='1h' AND time<=? ORDER BY time DESC LIMIT 48",(market,ts-3600)).fetchall()
        for row in reversed(rows):
            b=json.loads(row[0]);x=mdates.date2num(local(b['time']+1800))
            color='#16816b' if b['close']>=b['open'] else '#c23d4b'
            candle_ax.vlines(x,b['low'],b['high'],color=color,lw=1)
            candle_ax.add_patch(Rectangle((x-.014,min(b['open'],b['close'])),.028,max(abs(b['close']-b['open']),b['close']*.00002),facecolor=color))
        if not rows:candle_ax.text(.02,.5,'Bu işlem için kayıtlı gerçek mum verisi yok.',transform=candle_ax.transAxes)
        candle_ax.autoscale_view();candle_ax.grid(alpha=.15)
        candle_ax.set_title('Binance / USDT — gerçek saatlik mumlar (Paribu TL fiyatı değildir)',loc='left',fontsize=10)
        candle_ax.set_ylabel('USDT');candle_ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3,maxticks=5))
        candle_ax.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m %H:%M',tz=TZ))
    balance = fig.add_subplot(grid[3 if is_v3 else 2])
    prior = [t for t in history if t['id']<=trade['id']]
    balance.step([local(t['ts']) for t in prior],[t['realized_total_tl'] for t in prior],where='post',color='#743db3',lw=2,marker='o',markersize=4)
    balance.set_xlim(local(prior[0]['ts']-30),local(max(ts,prior[0]['ts']+30)+30))
    balance.axhline(0,color='#8997a7',lw=.8)
    balance.set_title('Birikimli gerçekleşmiş kâr / zarar',loc='left',fontsize=12)
    balance.set_ylabel('TL'); balance.grid(alpha=.16)
    balance.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3,maxticks=5))
    balance.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S' if ts-prior[0]['ts']<300 else '%H:%M',tz=TZ))
    foot = fig.add_subplot(grid[4 if is_v3 else 3]); foot.axis('off')
    pnl = 'Pozisyon açıldı; satış sonucu henüz yok.' if side=='ALIŞ' else f'Bu satış: {money(trade["pnl"])}  /  %{trade["net_return_pct"]:.2f}'
    text = (f'Gerçekleşme: {trade["price"]:.8g} TL × {trade["gross_quantity"]:.8g}    |    Komisyon: {money(trade["fee"])} karşılığı\n'
            f'{pnl}\nKalan nakit: {money(trade["cash_after_tl"])}    |    Birikimli gerçekleşmiş sonuç: {money(trade["realized_total_tl"])}\n'
            f'Her yön için %{trade["assumed_fee_each_side"]*100:g} varsayım; kesinti: {trade["fee_quantity"]:.8g} {trade["fee_currency"]}. TL çizgisi örneklenmiş fiyattır.\n'
            'Kesikli seviyeler net getiri referansıdır; derinlik ve gecikme nedeniyle gerçekleşme değişebilir.')
    if is_v3:
        text+=f'\n−%2/+%3 otomatik satış değildir; acil çıkış sınırı net %{trade["hard_stop_net_return"]*100:g}. Küresel kapsam kısmidir.'
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
            text = (f'# SANAL İŞLEM — BÖRÜ {market_label(trade)} {side}\n\n'
                    f'**Sanal işlem.** {trade["timestamp_local"]}\n\n'
                    f'![İşlem grafiği]({paths["png"].name})\n\n'
                    f'| Alan | Değer |\n|---|---|\n| İşlem kimliği | {ident} |\n'
                    f'| Fiyat | {trade["price"]:.8g} TL |\n| Brüt işlem miktarı | {trade["gross_quantity"]:.8g} |\n'
                    f'| Net alınan miktar | {trade["net_acquired_quantity"] if side=="ALIŞ" else "—"} |\n'
                    f'| İşlem tutarı | {money(trade["notional_tl"])} |\n| Komisyon TL karşılığı | {money(trade["fee"])} |\n'
                    f'| Komisyon kesintisi | {trade["fee_quantity"]:.8g} {trade["fee_currency"]} |\n'
                    f'| Bu satışın net sonucu | {pnl} |\n| Kalan nakit | {money(trade["cash_after_tl"])} |\n'
                    f'| Birikimli gerçekleşmiş sonuç | {money(trade["realized_total_tl"])} |\n'
                    f'| Üründe kalan miktar | {trade["remaining_qty"]:.8g} |\n\n'
                    f'Gerekçe: {trade["reason_tr"]}\n\n'
                    f'Strateji: {trade["strategy_version"]}; eşik davranışı: {trade["threshold_mode"]}.\n\n'
                    f'Karar verileri: {json.dumps(trade["decision_context"],ensure_ascii=False)}\n\n'
                    f'Gösterge değerleri: {json.dumps(trade["indicators"],ensure_ascii=False)}\n\n'
                    f'Komisyon her yön için %{trade["assumed_fee_each_side"]*100:g} varsayılmıştır; kişisel oran doğrulanmamıştır. '
                    'Alışta coin olarak kesilen komisyon TL nakitten ikinci kez düşülmez. Kalan nakit, toplam portföy değeri değildir. '
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
