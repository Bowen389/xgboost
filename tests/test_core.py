import copy
import unittest
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from src.common import TZ, CFG
from src.quotes import Quote, parse
from src.broker import initial_account, order, match, fee, mark
from src.model import features, select

WHEN=datetime(2026,10,28,9,31,tzinfo=TZ)

def quote(**kwargs):
    values=dict(code='sh.600000',name='浦发银行',timestamp=WHEN,last=10.,previous=10.,open=10.,
                volume=100000,upper=11.,lower=9.,bids=[(9.99,100000)],asks=[(10.,100000)])
    values.update(kwargs)
    return Quote(**values)

class Execution(unittest.TestCase):
    def test_no_fill_without_post_submission_trade(self):
        a=initial_account();order(a,'sh.600000','BUY',1000,'2026-10-28','test');o=a['orders'][0]
        match(a,o,quote(),WHEN)
        self.assertEqual(o['filled'],0)
        later=WHEN+timedelta(seconds=10)
        match(a,o,quote(timestamp=later,volume=110000),later)
        self.assertEqual(o['filled'],100)  # 1% of observed 10k shares
        self.assertEqual(a['positions']['sh.600000']['quantity'],100)
        self.assertGreater(a['cash'],0)
        self.assertLess(a['cash'],200000-1000)
        match(a,o,quote(timestamp=later,volume=110000),later)
        self.assertEqual(o['filled'],100)
    def test_stale_limit_st_missing(self):
        for q in [None,quote(timestamp=WHEN-timedelta(seconds=31)),quote(last=11),quote(name='*ST测试')]:
            a=initial_account();order(a,'sh.600000','BUY',1000,'2026-10-28','test')
            match(a,a['orders'][0],q,WHEN)
            self.assertEqual(a['fills'],[])
    def test_T1_and_corporate_freeze(self):
        for day,freeze in [('2026-10-28',False),('2026-10-27',True)]:
            a=initial_account();a['positions']['sh.600000']=dict(quantity=100,cost=1000,bought_day=day,reconcile_required=freeze)
            order(a,'sh.600000','SELL',100,'2026-10-28','test')
            for n in [0,10]:
                t=WHEN+timedelta(seconds=n);match(a,a['orders'][0],quote(timestamp=t,volume=100000+n*100000),t)
            self.assertEqual(a['fills'],[])
    def test_cash_and_partial_commission(self):
        a=initial_account();a['cash']=1010
        order(a,'sh.600000','BUY',1000,'2026-10-28','test')
        for n in [0,10,20]:
            t=WHEN+timedelta(seconds=n);match(a,a['orders'][0],quote(timestamp=t,volume=100000+n*100000),t)
        self.assertEqual(a['positions']['sh.600000']['quantity'],100)
        self.assertGreaterEqual(a['cash'],0)
        self.assertEqual(a['orders'][0]['fees'],fee(a['orders'][0]['gross'],'BUY'))
    def test_accounts_independent(self):
        a,b=initial_account(),initial_account();a['cash']=0;a['orders'].append({})
        self.assertEqual(b['cash'],200000);self.assertEqual(b['orders'],[])
    def test_commission_minimum_and_tax(self):
        self.assertEqual(fee(1000,'BUY'),5.01)
        self.assertEqual(fee(1000,'SELL'),5.51)

class Research(unittest.TestCase):
    def test_features_do_not_use_future(self):
        n=180
        f=pd.DataFrame(dict(date=pd.date_range('2025-01-01',periods=n).strftime('%Y-%m-%d'),code=['sh.600000']*n,
            close=np.arange(n)+10,preclose=np.arange(n)+9,open=np.arange(n)+10,high=np.arange(n)+11,
            low=np.arange(n)+9,volume=[10000]*n,amount=[3e7]*n,turn=[1.]*n,tradestatus=[1]*n,isST=[0]*n,pctChg=[.1]*n))
        before=features(f.iloc[:150]).iloc[-1]
        f.loc[150:,'pctChg']=10
        after=features(f).iloc[149]
        self.assertEqual(before.ret_120,after.ret_120)
        self.assertEqual(before.vol60,after.vol60)
    def test_threshold_empty_and_equal_weight(self):
        f=pd.DataFrame([dict(code=f'sh.{600000+i}',name='test',score=s,vol60=.02) for i,s in enumerate([.8,.76,.7])])
        chosen=select(f,'monthly075',{})
        self.assertEqual(len(chosen),2);self.assertEqual(sum(r['weight'] for r in chosen),1)
        self.assertEqual(select(f.assign(score=.74),'monthly075',{}),[])
    def test_v2_retains_top25_and_caps20(self):
        f=pd.DataFrame([dict(code=f'sh.{600000+i}',name='test',score=1-i*.01,vol60=.01+i*.001) for i in range(30)])
        chosen=select(f,'v2',{'sh.600024':{}})
        self.assertEqual(len(chosen),20);self.assertIn('sh.600024',[r['code'] for r in chosen])
        self.assertAlmostEqual(sum(r['weight'] for r in chosen),1)

if __name__=='__main__':
    unittest.main()
