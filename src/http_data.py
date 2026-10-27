"""Public Sina HTTPS fallback. No token and no fabricated historical ST flags.

Name history has no effective dates: conservatively exclude the entire security
if any disclosed name was ST/PT/delisting, rather than pretending to know daily ST.
"""
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO
import pandas as pd
import requests
from .common import ROOT,CFG,now,read_json,write_json,mainboard,unsafe_name

_local=threading.local()

def get(url):
    for attempt in range(3):
        try:
            r=requests.get(url,timeout=20,headers={'Referer':'https://finance.sina.com.cn/'})
            r.raise_for_status()
            return r
        except Exception:
            if attempt==2:
                raise
            time.sleep(1+attempt)


def decode_js(text):
    from akshare.stock.cons import hk_js_decode
    from py_mini_racer import MiniRacer
    if not hasattr(_local,'js'):
        _local.js=MiniRacer()
        _local.js.eval(hk_js_decode)
    encoded=text.split('=',1)[1].split(';',1)[0].strip().strip('"')
    return _local.js.call('d',encoded)


def calendar():
    r=get('https://finance.sina.com.cn/realstock/company/klc_td_sh.txt')
    values=decode_js(r.text)
    days=sorted(set(pd.to_datetime(values).strftime('%Y-%m-%d')))
    if not days or max(days)<now().date().isoformat():
        raise RuntimeError('Sina exchange calendar expired')
    write_json(ROOT/'data/calendar.json',days)
    return days


def master():
    url='https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'
    count_text=get(url+'Market_Center.getHQNodeStockCount?node=hs_a').text
    count=int(re.search(r'\d+',count_text).group())
    def page(i):
        return get(url+f'Market_Center.getHQNodeData?page={i}&num=80&sort=symbol&asc=1&node=hs_a&symbol=&_s_r_a=page').json()
    rows=[]
    with ThreadPoolExecutor(max_workers=3) as pool:
        for values in pool.map(page,range(1,math.ceil(count/80)+1)):
            rows.extend(values or [])
    if len({r['symbol'] for r in rows})<count*.97:
        raise RuntimeError('Incomplete Sina security master pages')
    output=[]
    for r in rows:
        symbol=r['symbol']
        code=symbol[:2]+'.'+symbol[2:]
        if mainboard(code):
            output.append(dict(code=code,code_name=r['name'],ipoDate='',outDate='',type='1'))
    if len(output)<CFG['min_universe']:
        raise RuntimeError('Insufficient HTTP mainboard master')
    return pd.DataFrame(output).drop_duplicates('code')


def json_assignment(text):
    return json.JSONDecoder().raw_decode(text.split('=',1)[1].lstrip())[0]


def download_one(row,asof):
    from akshare.stock.cons import zh_sina_a_stock_amount_url
    from akshare.utils import demjson
    code=row['code'];symbol=code.replace('.','')
    daily_path=ROOT/f'data/daily/{code}.csv.gz'
    daily_path.parent.mkdir(parents=True,exist_ok=True)
    proof_path=ROOT/f'data/profiles/{code}.json'
    proof=read_json(proof_path)
    # Refresh name evidence monthly and whenever current name changes.
    if not proof or proof['checked_month']!=asof[:7] or proof['current_name']!=row['code_name']:
        r=get(f'https://vip.stock.finance.sina.com.cn/corp/go.php/vCI_CorpInfo/stockid/{code[3:]}.phtml')
        r.encoding='gb18030'
        tables=pd.read_html(StringIO(r.text))
        names=None
        for table in tables:
            if table.shape[1]<2:
                continue
            for left,right in table.iloc[:,:2].itertuples(index=False,name=None):
                if '证券简称更名历史' in str(left):
                    names=[] if pd.isna(right) else str(right).split()
                    break
            if names is not None:
                break
        if names is None:
            raise RuntimeError('Missing former-name evidence; cannot assert historical non-ST')
        names=list(dict.fromkeys(names+[row['code_name']]))
        proof=dict(checked_month=asof[:7],current_name=row['code_name'],names=names,
                   excluded_ever_ST=any(unsafe_name(n) for n in names))
        write_json(proof_path,proof)
    # If already complete, no daily/factor/share requests are repeated.
    if daily_path.exists():
        old=pd.read_csv(daily_path,dtype={'code':str})
        if not old.empty and old.date.max()==asof and 'http_source' in old:
            old['isST']=int(proof['excluded_ever_ST'])
            old.to_csv(daily_path,index=False,compression='gzip')
            return dict(row,ipoDate=str(old.ipoDate.iloc[0]),fresh=True,excluded=proof['excluded_ever_ST'])
    raw=decode_js(get(f'https://finance.sina.com.cn/realstock/company/{symbol}/hisdata_klc2/klc_kl.js').text)
    df=pd.DataFrame(raw)
    if df.empty or not {'date','open','high','low','close','volume','amount'}.issubset(df.columns):
        raise RuntimeError('Missing Sina exact OHLCV/amount')
    df['date']=pd.to_datetime(df.date).dt.strftime('%Y-%m-%d')
    df=df[df.date.le(asof)].sort_values('date').drop_duplicates('date')
    ipo=str(df.date.min()) # observed first trading bar; conservative listing-age proxy
    factors=json_assignment(get(f'https://finance.sina.com.cn/realstock/company/{symbol}/hfq.js').text)
    if 'data' not in factors:
        raise RuntimeError('Missing adjustment-factor payload')
    fac=pd.DataFrame(factors['data'])
    if fac.empty:
        df['factor']=1.
    else:
        fac=fac.rename(columns={'d':'date','f':'factor'})
        fac['factor']=pd.to_numeric(fac.factor,errors='raise')
        if not (fac.factor>0).all():
            raise RuntimeError('Invalid historical adjustment factor')
        # Index union forward-fill uses only the latest factor effective ON/BEFORE
        # each bar. Future factors never backfill earlier history.
        s=fac.drop_duplicates('date').set_index('date').factor.sort_index()
        df['factor']=s.reindex(sorted(set(s.index)|set(df.date))).ffill().reindex(df.date).to_numpy()
        if df.factor.isna().any():
            raise RuntimeError('No historical adjustment factor before first bar')
    total_price=df.close.astype(float)*df.factor
    df['pctChg']=total_price.pct_change(fill_method=None)*100
    df['preclose']=df.close.astype(float)/(1+df.pctChg/100)
    r=get(zh_sina_a_stock_amount_url.format(symbol,symbol))
    shares=pd.DataFrame(demjson.decode(r.text[r.text.find('['):r.text.rfind(']')+1]))
    if shares.empty or shares.shape[1]!=2:
        raise RuntimeError('Missing dated floating-share history')
    shares.columns=['date','shares']
    shares['shares']=pd.to_numeric(shares.shares,errors='raise')*10000
    shares.date=pd.to_datetime(shares.date).dt.strftime('%Y-%m-%d')
    shares=shares.drop_duplicates('date').set_index('date').shares.sort_index()
    values=shares.reindex(sorted(set(shares.index)|set(df.date))).ffill().reindex(df.date).to_numpy()
    df['turn']=df.volume.astype(float)/values*100
    df['code']=code
    df['isST']=int(proof['excluded_ever_ST'])
    df['tradestatus']=(df.volume.gt(0)&df.close.gt(0)).astype(int)
    df['ipoDate']=ipo
    df['http_source']='Sina_raw_ohlcv_with_dated_hfq_and_float_shares'
    df=df[df.date.ge(CFG['data_start'])]
    columns=['date','code','open','high','low','close','preclose','volume','amount','turn','tradestatus','pctChg','isST','ipoDate','http_source']
    if df.empty:
        raise RuntimeError('Empty HTTP history')
    temp=daily_path.with_suffix('.tmp.gz')
    df[columns].to_csv(temp,index=False,compression='gzip')
    temp.replace(daily_path)
    return dict(row,ipoDate=ipo,fresh=bool(df.date.max()==asof),excluded=proof['excluded_ever_ST'])


def update(primary_error):
    from .data import expected_asof
    (ROOT/'data/daily').mkdir(parents=True,exist_ok=True)
    days=calendar()
    asof=expected_asof(days)
    listing=master()
    write_json(ROOT/'reports/data_quality.json',dict(asof=asof,ready=False,status='HTTP_DOWNLOAD_IN_PROGRESS',source='Sina HTTPS',primary_error=primary_error))
    rows=[];failures=[]
    started=now()
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks={pool.submit(download_one,row,asof):row for row in listing.to_dict('records')}
        for task in as_completed(tasks):
            row=tasks[task]
            try:
                rows.append(task.result())
            except Exception as exc:
                failures.append(dict(code=row['code'],error=f'{type(exc).__name__}: {exc}'))
            n=len(rows)+len(failures)
            if n%100==0:
                print(f'HTTP_DATA {n}/{len(listing)} successful={len(rows)} failures={len(failures)}',flush=True)
                write_json(ROOT/'reports/download_progress.json',dict(asof=asof,total=len(listing),completed=n,successful=len(rows),failures=len(failures),updated_at=now().isoformat()))
    good=pd.DataFrame(rows)
    if good.empty:
        raise RuntimeError('All HTTP symbols failed')
    good.drop(columns=['fresh','excluded']).to_csv(ROOT/'data/master.csv',index=False)
    fresh=int(good.fresh.sum())
    audit=dict(asof=asof,checked_at=now().isoformat(),started_at=started.isoformat(),active=len(listing),fresh=fresh,
        coverage=fresh/len(listing),failures=failures,excluded_ever_ST=int(good.excluded.sum()),source='Sina HTTPS',primary_error=primary_error,
        limitations=['Current master omits already delisted stocks: survivorship bias in reconstructed training.',
                    'Historical daily ST dates missing: securities with any ST/PT/delisting name in disclosed history are conservatively excluded in full.',
                    'Listing age uses earliest observed trading bar; may conservatively delay eligibility.'])
    audit['ready']=len(listing)>=CFG['min_universe'] and audit['coverage']>=CFG['min_coverage']
    write_json(ROOT/'reports/data_quality.json',audit)
    if not audit['ready']:
        raise RuntimeError(f'HTTP data coverage blocked: {fresh}/{len(listing)}; inspect reports/data_quality.json')
    return asof
