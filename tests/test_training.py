"""Synthetic integration test: a working model is not a claimed backtest."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
import src.model as module
from src.common import CFG

class TrainingIntegration(unittest.TestCase):
    def test_complete_labels_and_both_accounts(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for s in ['data/daily','reports','state']:
                (root/s).mkdir(parents=True)
            (root/'config.json').write_text(json.dumps(CFG))
            dates=pd.bdate_range('2023-10-02','2026-10-27').strftime('%Y-%m-%d')
            rng=np.random.default_rng(10);master=[]
            for i in range(300):
                code=f'sh.{600000+i}'
                master.append(dict(code=code,code_name=f'test{i}',ipoDate='2000-01-01',outDate=''))
                r=rng.normal(0,.02,len(dates));price=10*np.cumprod(1+r)
                f=pd.DataFrame(dict(date=dates,code=code,open=price,high=price*1.01,low=price*.99,
                    close=price,preclose=price/(1+r),pctChg=r*100,amount=3e7,turn=1.,volume=3e6,tradestatus=1,isST=0))
                f.to_csv(root/f'data/daily/{code}.csv.gz',index=False)
            pd.DataFrame(master).to_csv(root/'data/master.csv',index=False)
            with patch.object(module,'ROOT',root),patch.object(module,'CFG',{**CFG,'min_universe':20}):
                plans=module.train('2026-10-27')
            self.assertEqual(set(plans),{'v2','monthly075'})
            self.assertEqual(len(plans['v2']['targets']),20)
            self.assertLessEqual(len(plans['monthly075']['targets']),10)
            for key,p in plans.items():
                self.assertLess(p['max_label_end'],'2026-10-01')
                self.assertEqual(len(p['training_months']),CFG['accounts'][key]['train_months'])
                self.assertTrue((root/f'models/{key}.json').exists())
