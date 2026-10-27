"""Explicit reconstruction. No archived HTML holdings are used as orders."""
import hashlib
import numpy as np
import pandas as pd
from .common import ROOT, CFG, now, read_json, write_json

FEATURES = [f'ret_{n}' for n in (5,10,20,60,120)] + ['vol20','vol60','ma20_gap','ma60_gap','range20','turn20','amount_ratio','amplitude20','down_vol20']

def features(df):
    df = df.sort_values('date').copy()
    for c in df.columns.difference(['date','code']):
        df[c] = pd.to_numeric(df[c], errors='coerce')
    # Daily pctChg uses exchange reference price, preventing ex-dividend raw gaps
    # from masquerading as momentum. Only information through each row is used.
    r = df.pctChg / 100
    price = (1+r).cumprod()
    for n in (5,10,20,60,120):
        df[f'ret_{n}'] = price.pct_change(n, fill_method=None)
    for n in (20,60):
        df[f'vol{n}'] = r.rolling(n).std()
        df[f'ma{n}_gap'] = price/price.rolling(n).mean()-1
    df['range20'] = price/price.rolling(20).max()-1
    df['turn20'] = df.turn.rolling(20).mean()
    df['amount20'] = df.amount.rolling(20).mean()
    df['amount_ratio'] = df.amount.rolling(5).mean()/df.amount20
    df['amplitude20'] = ((df.high-df.low)/df.preclose).rolling(20).mean()
    df['down_vol20'] = r.clip(upper=0).rolling(20).std()
    df['total_return_index'] = price
    return df.replace([np.inf,-np.inf], np.nan)

def select(ranking, account, positions):
    spec = CFG['accounts'][account]
    ranked = ranking[ranking.score.ge(spec['threshold'])] if spec['threshold'] is not None else ranking
    n = spec['max_positions']
    keep = [c for c in ranked.head(n+spec['buffer']).code if c in positions][:n]
    chosen = keep + [c for c in ranked.code if c not in keep][:n-len(keep)]
    rows = ranked.set_index('code').loc[chosen].copy()
    if rows.empty:
        return []
    if spec['weight'] == 'equal':
        rows['weight'] = 1/len(rows)
    else:
        w = 1/rows.vol60.clip(lower=0.001)
        w = (w/w.sum()).clip(0.5/len(rows),1.5/len(rows))
        rows['weight'] = w/w.sum()
    return rows.reset_index()[['code','name','score','vol60','weight']].to_dict('records')

def train(asof, model_month=None):
    from xgboost import XGBClassifier
    (ROOT/'models').mkdir(parents=True, exist_ok=True)
    master = pd.read_csv(ROOT/'data/master.csv', keep_default_na=False).set_index('code')
    monthly, current = [], []
    for path in sorted((ROOT/'data/daily').glob('*.csv.gz')):
        raw = pd.read_csv(path, dtype={'code':str})
        raw = raw[raw.date.le(asof)]
        if raw.empty:
            continue
        f = features(raw)
        code = str(raw.code.iloc[0])
        if code not in master.index:
            continue
        info = master.loc[code]
        f['eligible'] = (f.isST.eq(0) & f.tradestatus.eq(1) & f.amount20.ge(CFG['min_amount20']) &
                         (pd.to_datetime(f.date)-pd.Timestamp(info.ipoDate)).dt.days.ge(CFG['min_listing_days']))
        f['month'] = f.date.str[:7]
        m = f.groupby('month', sort=True).tail(1).copy()
        m['label_end'] = m.date.shift(-1)
        m['future_return'] = m.total_return_index.shift(-1)/m.total_return_index-1
        consecutive = pd.to_datetime(m.label_end).dt.to_period('M') == pd.to_datetime(m.date).dt.to_period('M') + 1
        m.loc[~consecutive,'future_return'] = np.nan
        monthly.append(m[m.eligible].assign(code=code))
        if f.date.iloc[-1] == asof and bool(f.eligible.iloc[-1]) and (not info.outDate or info.outDate > asof):
            current.append(f.tail(1).assign(code=code, name=info.code_name))
    if not monthly or not current:
        raise RuntimeError('No complete feature data')
    sample = pd.concat(monthly, ignore_index=True)
    # A label must end in a fully closed month; an unfinished current-month
    # endpoint is never accepted, even though daily data already exists.
    month = model_month or asof[:7]
    sample = sample[sample.label_end.notna() & sample.label_end.str[:7].lt(month)]
    sample['rank'] = sample.groupby('month').future_return.rank(pct=True)
    sample['y'] = np.where(sample['rank'].ge(.7),1,np.where(sample['rank'].le(.3),0,np.nan))
    sample = sample.dropna(subset=FEATURES+['y'])
    live = pd.concat(current, ignore_index=True).dropna(subset=FEATURES)
    if len(live) < CFG['min_universe'] * 0.5:
        raise RuntimeError(f'Insufficient eligible feature universe: {len(live)}')
    ledger = read_json(ROOT/'state/ledger.json', {'accounts':{}})
    provenance = hashlib.sha256((ROOT/'config.json').read_bytes() + '\n'.join(FEATURES).encode()).hexdigest()
    plans = {}
    for account, spec in CFG['accounts'].items():
        months = sorted(sample.month.unique())[-spec['train_months']:]
        if len(months) != spec['train_months']:
            raise RuntimeError(f'{account}: incomplete training months {months}')
        trainset = sample[sample.month.isin(months)]
        if len(trainset) < 1000 or trainset.y.nunique() < 2:
            raise RuntimeError('Insufficient training rows/classes')
        model = XGBClassifier(n_estimators=120,max_depth=3,learning_rate=.05,subsample=1,
                              colsample_bytree=1,random_state=42,n_jobs=2,eval_metric='logloss')
        model.fit(trainset[FEATURES], trainset.y.astype(int))
        model.save_model(ROOT/f'models/{account}.json')
        ranking = live[['code','name','vol60']].copy()
        ranking['score'] = model.predict_proba(live[FEATURES])[:,1]
        ranking = ranking.sort_values(['score','code'], ascending=[False,True])
        ranking.to_csv(ROOT/f'reports/{account}_ranking.csv',index=False)
        positions = ledger['accounts'].get(account,{}).get('positions',{})
        plans[account] = dict(targets=select(ranking,account,positions),training_months=months,
            training_rows=len(trainset),max_label_end=str(trainset.label_end.max()),eligible_count=len(live))
    write_json(ROOT/'state/plan.json',dict(asof=asof,model_month=month,created_at=now().isoformat(),
        version=CFG['version'],config_hash=provenance,accounts=plans))
    return plans
