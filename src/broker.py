"""Independent paper accounts. Orders/fills/cash in one atomic ledger."""
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING, ROUND_FLOOR
from .common import CFG, unsafe_name


def cent(value, side=None):
    mode = ROUND_CEILING if side == 'BUY' else ROUND_FLOOR if side == 'SELL' else ROUND_HALF_UP
    return float(Decimal(str(value)).quantize(Decimal('.01'),rounding=mode))


def initial_account():
    return dict(cash=float(CFG['initial_cash']),positions={},orders=[],fills=[],events=[],
                cycle=None,nav=float(CFG['initial_cash']),marks={},snapshots={},closed_days=[])


def fee(gross,side):
    return cent(max(CFG['min_commission'],gross*CFG['commission']) +
                gross*CFG['transfer_fee'] + (gross*CFG['stamp_tax'] if side=='SELL' else 0))


def order(account, code, side, qty, day, reason):
    qty = int(qty)//100*100
    if qty <= 0 or any(o['code']==code and o['side']==side and o['status']=='OPEN' for o in account['orders']):
        return
    account['orders'].append(dict(id=f'{day}-{len(account["orders"])+1}', code=code,side=side,
        quantity=qty,filled=0,status='OPEN',day=day,reason=reason,gross=0.,fees=0.,last_volume=None,last_stamp=None))


def rejection(q, side, when):
    if q is None:
        return 'missing_quote'
    age = (when-q.timestamp).total_seconds()
    if q.timestamp.date()!=when.date() or not 0<=age<=CFG['max_quote_age_seconds']:
        return 'stale_quote'
    if q.last<=0 or q.open<=0 or q.volume<=0:
        return 'suspended_or_no_trade'
    if side=='BUY' and unsafe_name(q.name):
        return 'ST_or_delisting'
    if side=='BUY' and q.last>=q.upper-.005:
        return 'limit_up_no_queue_assumption'
    if side=='SELL' and q.last<=q.lower+.005:
        return 'limit_down_no_queue_assumption'
    return None


def match(account, o, q, when):
    reason = rejection(q,o['side'],when)
    if reason:
        o['blocked_reason']=reason
        return
    code,side = o['code'],o['side']
    p = account['positions'].get(code)
    if p and p.get('reconcile_required'):
        o['blocked_reason']='corporate_action_reconciliation'
        return
    if side=='SELL' and (not p or p['bought_day']>=when.date().isoformat()):
        o['blocked_reason']='T_plus_1'
        return
    # Only volume observed AFTER order submission/previous match can be used.
    if o['last_volume'] is None:
        o['last_volume'],o['last_stamp']=q.volume,q.timestamp.isoformat()
        o['blocked_reason']='waiting_for_post_order_volume'
        return
    if o['last_stamp']==q.timestamp.isoformat():
        o['blocked_reason']='unchanged_quote'
        return
    delta=max(0,q.volume-o['last_volume'])
    o['last_volume'],o['last_stamp']=q.volume,q.timestamp.isoformat()
    remaining=o['quantity']-o['filled']
    volume_cap=int(delta*CFG['volume_participation'])//100*100
    qty, gross = 0,0.
    levels=q.asks if side=='BUY' else q.bids
    for price,size in levels:
        if price<=0 or size<=0:
            continue
        px=cent(price*(1+CFG['slippage'] if side=='BUY' else 1-CFG['slippage']),side)
        if not q.lower<=px<=q.upper:
            continue
        take=min(remaining-qty,volume_cap-qty,int(size*CFG['book_participation'])//100*100)
        if side=='SELL':
            take=min(take, p['quantity']-qty)
        while take>0 and side=='BUY':
            total_gross=o['gross']+gross+take*px
            marginal_fee=fee(total_gross,side)-o['fees']
            if gross+take*px+marginal_fee<=account['cash']+.001:
                break
            take-=100
        if take>0:
            qty+=take
            gross+=take*px
        if qty>=min(remaining,volume_cap):
            break
    if not qty:
        o['blocked_reason']='insufficient_new_volume_book_or_cash'
        return
    gross=cent(gross)
    fees=cent(fee(o['gross']+gross,side)-o['fees'])
    if side=='BUY':
        account['cash']=cent(account['cash']-gross-fees)
        if p:
            p['cost']+=gross+fees
            p['quantity']+=qty
            # Rebalance additions occur once per cycle; conservatively lock
            # the entire position until next day if topped up today.
            p['bought_day']=when.date().isoformat()
        else:
            account['positions'][code]=dict(quantity=qty,cost=gross+fees,bought_day=when.date().isoformat(),name=q.name)
    else:
        account['cash']=cent(account['cash']+gross-fees)
        cost=p['cost']*qty/p['quantity']
        p['cost']-=cost
        p['quantity']-=qty
        if p['quantity']==0:
            del account['positions'][code]
    o['filled']+=qty
    o['gross']=cent(o['gross']+gross)
    o['fees']=cent(o['fees']+fees)
    o.pop('blocked_reason',None)
    if o['filled']==o['quantity']:
        o['status']='FILLED'
    account['fills'].append(dict(order_id=o['id'],timestamp=when.isoformat(),quote_time=q.timestamp.isoformat(),
        code=code,name=q.name,side=side,quantity=qty,price=gross/qty,gross=gross,fee=fees,
        cash_after=account['cash'],reason=o['reason'],source=q.source))


def expire(account,day):
    for o in account['orders']:
        if o['status']=='OPEN':
            o['status']='PARTIAL_CANCELLED' if o['filled'] else 'CANCELLED'
            o['cancelled_on']=day


def mark(account,quotes,when):
    stale=[]
    for code,p in account['positions'].items():
        q=quotes.get(code)
        if q and q.timestamp.date()==when.date() and 0<q.last and (when-q.timestamp).total_seconds()<=60:
            account['marks'][code]=dict(price=q.last,timestamp=q.timestamp.isoformat())
        elif code not in account['marks']:
            stale.append(code)
        else:
            stale.append(code)
    account['nav']=cent(account['cash']+sum(p['quantity']*account['marks'].get(c,{'price':p['cost']/p['quantity']})['price'] for c,p in account['positions'].items()))
    account['valuation_stale']=stale
    account['valued_at']=when.isoformat()
