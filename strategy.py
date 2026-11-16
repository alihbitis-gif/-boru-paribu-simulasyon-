"""Deterministic research policy for paper trading; outputs evidence, not probabilities."""


def compact(context):
    return {k:v for k,v in context.items() if k not in ('hours','days')}


def flow(context):
    tape=context.get('large_trades',{})
    chain=context.get('chain',{})
    # Different venues and quote assets stay separate; do not add them as one global flow.
    exchange_buy=tape.get('buy',0)>max(100000,tape.get('sell',0)*1.5)
    exchange_sell=tape.get('sell',0)>max(100000,tape.get('buy',0)*1.5)
    chain_buy=chain.get('fresh',False) and chain.get('buy',0)>max(100000,chain.get('sell',0)*1.5)
    chain_sell=chain.get('fresh',False) and chain.get('sell',0)>max(100000,chain.get('buy',0)*1.5)
    return exchange_buy or chain_buy,exchange_sell or chain_sell


def entry_decision(context,rebound):
    if not context.get('fresh'):return False,'RESEARCH_PENDING_OR_STALE'
    if not context.get('identity_verified'):return False,'ASSET_IDENTITY_UNCONFIRMED'
    if not context.get('trade_feed_fresh',False):return False,'TRADE_FEED_UNCONFIRMED'
    m=context['metrics'];p=m['patterns']
    buy_flow,sell_flow=flow(context)
    if buy_flow:return False,'LARGE_BUY_COUNTERTRADE_WAIT'
    if not rebound:return False,'NO_LOCAL_PRICE_RECOVERY'
    if p['bearish_engulfing'] or p['shooting_star']:return False,'BEARISH_CANDLE'
    volume=m.get('volume_ratio')
    if volume is None or volume<1:return False,'VOLUME_UNCONFIRMED'
    bullish=m['hour_trend_up'] and m['daily_trend_up'] and m['return_1h']>0
    reversal=(p['bullish_engulfing'] or p['hammer']) and m['return_1h']>0
    if sell_flow and (bullish or reversal):return True,'LARGE_SELL_ABSORPTION'
    if bullish and m['return_7d']>0:return True,'MULTITIMEFRAME_TREND'
    return False,'CANDLE_TREND_UNCONFIRMED'


def exit_decision(net_return,peak,context,hard_stop=-.05):
    if net_return<=hard_stop:return True,'HARD_RISK_EXIT'
    if not context.get('fresh') or not context.get('identity_verified'):
        return (True,'LOSS_EXIT_DATA_UNCONFIRMED') if net_return<=-.02 else (False,'WAIT_FOR_RESEARCH')
    m=context['metrics'];p=m['patterns']
    large_buy,_=flow(context)
    bearish=p['bearish_engulfing'] or p['shooting_star'] or (not m['hour_trend_up'] and m['return_1h']<0)
    upside=m['hour_trend_up'] and m['daily_trend_up'] and m['return_1h']>0 and not bearish
    # +3% is a review point, not a mandatory wait or an automatic sale.
    trail=max(.006,min(.018,m['atr_fraction']*.75))
    if peak>=.005 and net_return>0 and peak-net_return>=trail:return True,'TRAILING_PROFIT'
    if net_return>0 and large_buy:return True,'LARGE_BUY_COUNTERTRADE_PROFIT'
    if net_return>0 and bearish:return True,'EARLY_PROFIT_WEAKENING'
    if net_return>=.03 and not upside:return True,'PROFIT_UPSIDE_UNCONFIRMED'
    # Below -2%, carrying requires positive price evidence AND fresh chain coverage.
    if net_return<=-.02:
        if not context.get('chain',{}).get('fresh'):return True,'LOSS_EXIT_CHAIN_UNCONFIRMED'
        if not upside or large_buy:return True,'LOSS_EXIT_RESEARCH_WEAK'
        return False,'HOLD_RECOVERY_WITH_CHAIN_CONFIRMATION'
    if net_return<0 and bearish and not m['daily_trend_up']:return True,'EARLY_LOSS_TREND_BREAK'
    return False,'HOLD_UPSIDE' if upside else 'HOLD_REVIEW'


REASONS_TR={
 'LARGE_SELL_ABSORPTION':'Büyük satış gözlemi sonrası yerel fiyat toparlanması ve mum/hacim teyidi.',
 'MULTITIMEFRAME_TREND':'Saatlik, günlük ve 7 günlük eğilim; mum, hacim ve yerel fiyat teyidi.',
 'HARD_RISK_EXIT':'Net %5 deneysel azami zarar sınırı aşıldı.',
 'LOSS_EXIT_DATA_UNCONFIRMED':'Net zarar %2 inceleme seviyesinde; gerekli güncel araştırma verisi doğrulanamadı.',
 'LOSS_EXIT_CHAIN_UNCONFIRMED':'Net zarar %2 inceleme seviyesinde; ilgili varlık için güncel zincir teyidi yok.',
 'LOSS_EXIT_RESEARCH_WEAK':'Mum, eğilim veya büyük işlem verisi toparlanmayı desteklemiyor.',
 'TRAILING_PROFIT':'Görülen kâr zirvesinden oynaklığa göre belirlenen geri çekilme oluştu.',
 'LARGE_BUY_COUNTERTRADE_PROFIT':'Büyük alış gözlemine karşı, eldeki sanal pozisyonda net kâr alındı.',
 'EARLY_PROFIT_WEAKENING':'Mum/eğilim zayıfladığı için %3 beklenmeden net kâr alındı.',
 'PROFIT_UPSIDE_UNCONFIRMED':'Net %3 inceleme seviyesinde yükseliş desteği yetersiz bulundu.',
 'EARLY_LOSS_TREND_BREAK':'Saatlik ve günlük eğilim bozulduğu için %2 zarar beklenmeden çıkış kararı.',
 'STOP':'Net zarar kes eşiği tetiklendi.', 'TARGET':'Net kâr al eşiği tetiklendi.'}
