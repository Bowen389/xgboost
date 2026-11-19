import unittest
from datetime import datetime
from src.broker import mark
from src.common import TZ

class Valuation(unittest.TestCase):
    def account(self,day='2026-11-19'):
        return dict(cash=0.,positions={'sh.600000':dict(quantity=100,cost=1000.)},
                    marks={'sh.600000':dict(price=11.,timestamp=day+'T15:00:00+08:00',source='validated_daily_close')})
    def test_validated_same_day_close_is_valid_after_market(self):
        account=self.account()
        mark(account,{},datetime(2026,11,19,22,0,tzinfo=TZ))
        self.assertEqual(account['valuation_stale'],[])
        self.assertEqual(account['nav'],1100.)
    def test_previous_close_stays_stale(self):
        account=self.account('2026-11-18')
        mark(account,{},datetime(2026,11,19,22,0,tzinfo=TZ))
        self.assertEqual(account['valuation_stale'],['sh.600000'])
    def test_close_does_not_masquerade_as_intraday_live_quote(self):
        account=self.account()
        mark(account,{},datetime(2026,11,19,14,0,tzinfo=TZ))
        self.assertEqual(account['valuation_stale'],['sh.600000'])

if __name__=='__main__':unittest.main()
