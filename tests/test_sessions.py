import unittest
from unittest.mock import patch
from datetime import datetime
from src import app

class Sessions(unittest.TestCase):
    def test_late_task_preserves_success_and_accounts(self):
        ledger={'accounts':{'a':{'positions':{'stock':100}}},'status':'early success'}
        with patch.object(app,'load',return_value=ledger), patch.object(app,'save') as save, patch.object(app,'now',return_value=datetime(2026,10,29,15,2,tzinfo=app.TZ)), patch.object(app,'valid_inputs') as valid:
            app.session('open')
        self.assertEqual(ledger['status'],'early success')
        self.assertEqual(ledger['accounts']['a']['positions'],{'stock':100})
        self.assertEqual(ledger['sessions']['2026-10-29_open']['status'],'skipped')
        valid.assert_not_called()
        save.assert_called_once()

    def test_completed_session_is_idempotent(self):
        ledger={'sessions':{'2026-10-29_open':{'status':'completed'}}}
        with patch.object(app,'load',return_value=ledger), patch.object(app,'save') as save, patch.object(app,'now',return_value=datetime(2026,10,29,15,2,tzinfo=app.TZ)), patch.object(app,'valid_inputs') as valid:
            app.session('open')
        save.assert_not_called()
        valid.assert_not_called()
        self.assertEqual(ledger['sessions']['2026-10-29_open']['status'],'completed')
