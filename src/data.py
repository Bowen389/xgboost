"""BaoStock daily raw prices; sequential session, incremental per-symbol checkpoints."""
import socket
import time
from datetime import timedelta
import pandas as pd
from .common import ROOT, CFG, now, write_json, read_json, mainboard

FIELDS = 'date,code,open,high,low,close,preclose,volume,amount,turn,tradestatus,pctChg,isST'

def result_frame(rs):
    if rs.error_code != '0':
        raise RuntimeError(f'BaoStock {rs.error_code}: {rs.error_msg}')
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    if rs.error_code != '0':
        raise RuntimeError(f'BaoStock stream {rs.error_code}: {rs.error_msg}')
    return pd.DataFrame(rows, columns=rs.fields)

class Bao:
    def __enter__(self):
        import baostock as bs
        socket.setdefaulttimeout(30)
        self.bs = bs
        r = bs.login()
        if r.error_code != '0':
            raise RuntimeError(f'BaoStock login failed: {r.error_msg}')
        return self
    def __exit__(self, *args):
        self.bs.logout()
    def calendar(self, end=None):
        today = now().date()
        end = end or (today + timedelta(days=40)).isoformat()
        df = result_frame(self.bs.query_trade_dates(start_date=(today-timedelta(days=150)).isoformat(), end_date=end))
        if df.empty:
            raise RuntimeError('Empty exchange calendar')
        days = df.loc[df.is_trading_day.eq('1'), 'calendar_date'].tolist()
        write_json(ROOT/'data/calendar.json', days)
        return days

def session_dates():
    days = read_json(ROOT/'data/calendar.json', [])
    if not days or max(days) < now().date().isoformat():
        with Bao() as b:
            days = b.calendar()
    return days

def expected_asof(days):
    today = now().date().isoformat()
    candidates = [d for d in days if d < today or (d == today and now().hour >= 17)]
    if not candidates:
        raise RuntimeError('No completed trading session')
    return max(candidates)

def update_bao():
    folder = ROOT/'data/daily'
    folder.mkdir(parents=True, exist_ok=True)
    with Bao() as b:
        days = b.calendar()
        asof = expected_asof(days)
        master = result_frame(b.bs.query_stock_basic())
        master = master[master.code.map(mainboard) & master.type.eq('1')].copy()
        master.to_csv(ROOT/'data/master.csv', index=False)
        # Historical delisted securities are retained for training (survivorship mitigation).
        failures, eligible, fresh = [], 0, 0
        for i, row in enumerate(master.to_dict('records')):
            code = row['code']
            path = folder/f'{code}.csv.gz'
            old = pd.read_csv(path, dtype={'code':str}) if path.exists() else pd.DataFrame()
            start = CFG['data_start']
            if not old.empty:
                start = (pd.Timestamp(old.date.max()) + pd.Timedelta(days=1)).date().isoformat()
            try:
                active = row['ipoDate'] <= asof and (not row['outDate'] or row['outDate'] > asof)
                if active:
                    eligible += 1
                if start <= asof and (active or not row['outDate'] or start <= row['outDate']):
                    frame = None
                    for attempt in range(3):
                        try:
                            frame = result_frame(b.bs.query_history_k_data_plus(code, FIELDS, start_date=start, end_date=asof, frequency='d', adjustflag='3'))
                            break
                        except Exception:
                            if attempt == 2:
                                raise
                            time.sleep(1 + attempt)
                    if not frame.empty:
                        old = pd.concat([old, frame], ignore_index=True).drop_duplicates('date').sort_values('date')
                        tmp = path.with_suffix('.tmp.gz')
                        old.to_csv(tmp, index=False, compression='gzip')
                        tmp.replace(path)
                if active and not old.empty and old.date.max() == asof:
                    fresh += 1
            except Exception as exc:
                failures.append({'code': code, 'error': str(exc)})
            if i % 50 == 0:
                print(f'DATA {i}/{len(master)} fresh={fresh} failures={len(failures)}', flush=True)
        audit = dict(asof=asof, checked_at=now().isoformat(), active=eligible, fresh=fresh,
                     coverage=fresh/max(eligible,1), failures=failures, source='BaoStock raw daily adjustflag=3')
        audit['ready'] = eligible >= CFG['min_universe'] and audit['coverage'] >= CFG['min_coverage']
        write_json(ROOT/'reports/data_quality.json', audit)
        if not audit['ready']:
            raise RuntimeError(f'Data coverage blocked: {fresh}/{eligible}; see reports/data_quality.json')
    return asof


def update():
    try:
        return update_bao()
    except Exception as exc:
        print(f'PRIMARY_SOURCE_FAILED {type(exc).__name__}: {exc}; trying public HTTPS',flush=True)
        from .http_data import update as update_http
        return update_http(f'{type(exc).__name__}: {exc}')
