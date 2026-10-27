import argparse
from dataclasses import asdict
from datetime import datetime, time as dt_time, timedelta
import traceback
import time
import pandas as pd
from .common import ROOT, CFG, TZ, now, read_json, write_json
from .broker import initial_account, order, match, expire, mark, cent
from .report import export


def load():
    return read_json(ROOT/'state/ledger.json',dict(accounts={a:initial_account() for a in CFG['accounts']},status='已初始化；等待公开数据训练与启动时段'))


def save(ledger):
    write_json(ROOT/'state/ledger.json',ledger)
    export(ledger)


def prepare():
    from .data import update, session_dates
    from .model import train
    ledger=load()
    save(ledger)
    asof=update()
    days=session_dates()
    upcoming=min(d for d in days if d>asof)
    model_month=upcoming[:7]
    plan=read_json(ROOT/'state/plan.json')
    if not plan or plan['model_month']!=model_month:
        train(asof,model_month)
    symbols=set()
    plan=read_json(ROOT/'state/plan.json')
    for a in plan['accounts'].values():
        symbols.update(t['code'] for t in a['targets'])
    for a in ledger['accounts'].values():
        symbols.update(a['positions'])
    market={}
    for code in sorted(symbols):
        path=ROOT/f'data/daily/{code}.csv.gz'
        if not path.exists():
            continue
        df=pd.read_csv(path).sort_values('date')
        df=df[df.date.le(asof)]
        if df.empty:
            continue
        last=df.iloc[-1]
        market[code]=dict(date=str(last.date),close=float(last.close),preclose=float(last.preclose),
            isST=int(last.isST),tradestatus=int(last.tradestatus),yesterday_limit_up=bool(float(last.close)>=cent(float(last.preclose)*1.1)-.005))
        for a in ledger['accounts'].values():
            p=a['positions'].get(code)
            if not p:
                continue
            check=df[df.date.ge(p.get('corporate_checked',p['bought_day']))].copy()
            check['prior_raw']=df.close.shift(1).reindex(check.index)
            gaps=check[check.date.gt(p['bought_day']) & (check.preclose-check.prior_raw).abs().gt(.015)]
            if not gaps.empty:
                p['reconcile_required']=True
                p['reconcile_evidence']=gaps[['date','preclose','prior_raw']].to_dict('records')
            p['corporate_checked']=asof
            a['marks'][code]=dict(price=float(last.close),timestamp=str(last.date)+'T15:00:00+08:00')
    quality=read_json(ROOT/'reports/data_quality.json')
    write_json(ROOT/'state/market.json',dict(asof=asof,next_session=upcoming,calendar=days,quality=quality,symbols=market))
    ledger['status']=f'公开数据准备完成，截至{asof}；下个交易日{upcoming}。未产生任何模拟成交。'
    for a in ledger['accounts'].values():
        mark(a,{},now())
    save(ledger)


def valid_inputs(day):
    market=read_json(ROOT/'state/market.json')
    plan=read_json(ROOT/'state/plan.json')
    if not market or not plan:
        raise RuntimeError('BLOCKED: 缺少已验证数据或模型计划')
    if day not in market['calendar']:
        return None,None
    previous=max(d for d in market['calendar'] if d<day)
    if market['asof']!=previous or not market['quality']['ready']:
        raise RuntimeError(f'BLOCKED: 前一交易日数据不完整或过期 (asof={market["asof"]}, expected={previous})')
    if plan['asof']>=day or plan['model_month']!=day[:7]:
        raise RuntimeError('BLOCKED: 月度模型计划过期或未来信号')
    return market,plan


def rebalance(a,targets,quotes,day,cycle):
    # Fix budgets once. Blocked sells retain their economic exposure; no leverage.
    mark(a,quotes,now())
    capital=a['nav']*.995
    desired={t['code']:dict(t,budget=capital*t['weight']) for t in targets}
    a['target_budgets']=desired
    a['cycle']=cycle
    for code,p in a['positions'].items():
        q=quotes.get(code)
        budget=desired.get(code,{}).get('budget',0)
        if code not in desired:
            order(a,code,'SELL',p['quantity'],day,'monthly_remove')
        elif q and q.last>0:
            excess=p['quantity']-int(budget/q.last)//100*100
            order(a,code,'SELL',excess,day,'monthly_reduce')
    a['buy_day']=day


def buy_orders(a,quotes,day):
    if a.get('buy_day')!=day:
        return
    # Orders are day-only; missed/blocked buys do not refill on future days.
    for code,t in a.get('target_budgets',{}).items():
        if any(o['code']==code and o['side']=='BUY' and o['day']==day for o in a['orders']):
            continue
        q=quotes.get(code)
        if not q or q.last<=0:
            continue
        held=a['positions'].get(code,{}).get('quantity',0)
        target=int(t['budget']/(q.last*(1+CFG['slippage']+CFG['commission']+CFG['transfer_fee'])))//100*100
        order(a,code,'BUY',target-held,day,'initial_or_monthly_buy')


def tick(ledger,market,plan,quotes,when,phase):
    day=when.date().isoformat()
    is_first=day==min(d for d in market['calendar'] if d.startswith(day[:7]))
    for name,a in ledger['accounts'].items():
        for o in a['orders']:
            if o['status']=='OPEN' and o['day']!=day:
                o['status']='PARTIAL_CANCELLED' if o['filled'] else 'EXPIRED'
        first=a['cycle'] is None
        monthly=phase=='close' and is_first and a['cycle']!=day[:7]
        if (first and phase=='open') or monthly:
            rebalance(a,plan['accounts'][name]['targets'],quotes,day,day[:7])
        # Retry blocked exits from prior sessions. Never recreate buys.
        for code,p in list(a['positions'].items()):
            info=market['symbols'].get(code,{})
            q=quotes.get(code)
            if q and info.get('date')==market['asof'] and abs(q.previous-info['close'])>.015:
                p['reconcile_required']=True
                p['reconcile_evidence']={'date':day,'reference_price':q.previous,'previous_raw_close':info['close']}
            if info.get('isST')==1 or (q and ('ST' in q.name.upper() or '退' in q.name)):
                p['exit_reason']='became_ST'
            if phase=='close' and info.get('yesterday_limit_up') and q and q.last<q.upper-.005:
                p['exit_reason']='previous_limit_up_opened_near_close'
            if code not in a.get('target_budgets',{}) and a['cycle'] is not None:
                p['exit_reason']='monthly_remove'
            if p.get('exit_reason'):
                order(a,code,'SELL',p['quantity'],day,p['exit_reason'])
        # Sell first; pending unsold inventory still counts toward holding cap.
        for o in a['orders']:
            if o['status']=='OPEN' and o['side']=='SELL':
                match(a,o,quotes.get(o['code']),when)
        buy_orders(a,quotes,day)
        for o in a['orders']:
            if o['status']=='OPEN' and o['side']=='BUY':
                if o['code'] not in a['positions'] and len(a['positions'])>=CFG['accounts'][name]['max_positions']:
                    o['blocked_reason']='max_positions_unsold_inventory'
                    continue
                match(a,o,quotes.get(o['code']),when)
        mark(a,quotes,when)
    ledger['status']=f'运行中：{day} {phase}；仅使用当前有效盘口估算，未成交不回填。'


def session(phase):
    ledger=load()
    when=now()
    day=when.date().isoformat()
    if day<CFG['start_date']:
        ledger['status']='启动日期未到：'+CFG['start_date']
        save(ledger)
        return
    market,plan=valid_inputs(day)
    if market is None:
        ledger['status']=f'{day} 非交易日'
        save(ledger)
        return
    start,end=('09:30:05','09:40:00') if phase=='open' else ('14:55:00','14:59:40')
    start_dt=datetime.fromisoformat(day+'T'+start).replace(tzinfo=TZ)
    end_dt=datetime.fromisoformat(day+'T'+end).replace(tzinfo=TZ)
    if when>end_dt:
        ledger['status']=f'{phase}任务延迟至{when.isoformat()}，已错过窗口；不补记开盘/收盘成交。'
        save(ledger)
        return
    from .quotes import fetch
    folder=ROOT/'reports/quote_audit'
    folder.mkdir(parents=True,exist_ok=True)
    while now()<start_dt:
        time.sleep(min(20,max(0,(start_dt-now()).total_seconds())))
    while now()<end_dt:
        codes=set()
        for a in ledger['accounts'].values():
            codes.update(a['positions'])
            codes.update(o['code'] for o in a['orders'] if o['status']=='OPEN')
        for a in plan['accounts'].values():
            codes.update(t['code'] for t in a['targets'])
        try:
            quotes=fetch(sorted(codes)) if codes else {}
            when=now()
            if when>end_dt:
                break
            # Preserve exact parsed snapshots before matching; including stale ones.
            with (folder/f'{day}_{phase}.jsonl').open('a') as f:
                import json
                f.write(json.dumps({'received_at':when.isoformat(),'quotes':{c:dict(asdict(q),timestamp=q.timestamp.isoformat()) for c,q in quotes.items()}},ensure_ascii=False)+'\n')
            tick(ledger,market,plan,quotes,when,phase)
            save(ledger)
        except Exception as exc:
            ledger['status']=f'行情/撮合异常：{type(exc).__name__}: {exc}；未补记成交。'
            save(ledger)
            print(traceback.format_exc(),flush=True)
        time.sleep(10)
    for a in ledger['accounts'].values():
        expire(a,day)
        if phase=='close':
            navrow=dict(date=day,nav=a['nav'],cash=a['cash'],positions=len(a['positions']),stale=','.join(a.get('valuation_stale',[])))
            a['daily_nav']=[r for r in a.get('daily_nav',[]) if r['date']!=day]+[navrow]
    ledger['status']=f'{day} {phase}执行窗口结束；成交及未成交原因已写入独立账本。'
    save(ledger)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['init','prepare','open','close','report','probe'])
    args=p.parse_args()
    try:
        if args.command in ('init','report'):
            save(load())
        elif args.command=='prepare':
            prepare()
        elif args.command=='probe':
            from .quotes import fetch
            q=fetch(['sh.600000','sz.000001'])
            if len(q)!=2:
                raise RuntimeError('Live quote provider schema probe failed')
            write_json(ROOT/'reports/provider_probe.json',{c:dict(asdict(v),timestamp=v.timestamp.isoformat()) for c,v in q.items()})
        else:
            session(args.command)
    except Exception as exc:
        ledger=load()
        ledger['status']=f'BLOCKED {now().isoformat()}: {type(exc).__name__}: {exc}'
        save(ledger)
        raise

if __name__=='__main__':
    main()
