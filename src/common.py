import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / 'config.json').read_text())

def now():
    return datetime.now(TZ)

def read_json(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    temp.replace(path)

def mainboard(code):
    return code.startswith(('sh.600', 'sh.601', 'sh.603', 'sh.605', 'sz.000', 'sz.001', 'sz.002', 'sz.003'))

def unsafe_name(name):
    return any(x in name.upper() for x in ('ST', '退', 'PT'))
