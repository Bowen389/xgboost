"""Tencent timestamped five-level book. Values are validated, never backfilled."""
import re
from dataclasses import dataclass
from datetime import datetime
import requests
from .common import TZ, now

@dataclass
class Quote:
    code: str
    name: str
    timestamp: datetime
    last: float
    previous: float
    open: float
    volume: int
    upper: float
    lower: float
    bids: list
    asks: list
    source: str = 'Tencent public five-level snapshot'


def parse(text):
    quotes = {}
    for symbol, content in re.findall(r'v_([a-z]{2}\d{6})="([^"]*)"', text):
        f = content.split('~')
        if len(f) < 49:
            continue
        try:
            code = symbol[:2]+'.'+symbol[2:]
            stamp = datetime.strptime(f[30],'%Y%m%d%H%M%S').replace(tzinfo=TZ)
            q = Quote(code,f[1],stamp,float(f[3]),float(f[4]),float(f[5]),int(float(f[6])*100),
                      float(f[47]),float(f[48]),
                      [(float(f[i]),int(float(f[i+1])*100)) for i in range(9,19,2)],
                      [(float(f[i]),int(float(f[i+1])*100)) for i in range(19,29,2)])
            if q.previous > 0 and q.upper > q.lower > 0 and q.volume >= 0:
                quotes[code] = q
        except (ValueError,IndexError):
            continue
    return quotes


def fetch(codes):
    out = {}
    for i in range(0,len(codes),50):
        symbols = ','.join(c.replace('.','') for c in codes[i:i+50])
        r = requests.get('https://qt.gtimg.cn/q='+symbols, timeout=15,
                         headers={'Referer':'https://gu.qq.com/'})
        r.raise_for_status()
        r.encoding = 'gb18030'
        out.update(parse(r.text))
    return out
