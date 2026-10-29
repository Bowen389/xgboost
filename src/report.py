import csv
import io
from .common import ROOT, now

def export(ledger):
    lines=['# 双策略模拟仓','',f'更新时间：{now().isoformat()}','',
           '重建版；每仓初始资金20万元。成交是盘口约束下的估算，不是券商成交。','',
           '| 账户 | 现金 | 净值 | 收益 | 持仓数 | 累计成交片段 | 估值缺失/过期 |',
           '|---|---:|---:|---:|---:|---:|---|']
    for name,a in ledger['accounts'].items():
        lines.append(f'| {name} | {a["cash"]:,.2f} | {a["nav"]:,.2f} | {a["nav"]/200000-1:.2%} | {len(a["positions"])} | {len(a["fills"])} | {",".join(a.get("valuation_stale",[])) or "无"} |')
        folder=ROOT/'reports'/name
        folder.mkdir(parents=True,exist_ok=True)
        for filename,rows in [('fills',a['fills']),('orders',a['orders']),('positions',[dict(code=c,**p) for c,p in a['positions'].items()]),('daily_nav',a.get('daily_nav',[]))]:
            keys=list(dict.fromkeys(k for r in rows for k in r))
            out=io.StringIO()
            writer=csv.DictWriter(out,fieldnames=keys or ['status'])
            writer.writeheader()
            writer.writerows(rows)
            (folder/f'{filename}.csv').write_text(out.getvalue())
    lines+=['','## 执行状态','',ledger.get('status','尚未启动'),'',
            '数据覆盖与错误：`data_quality.json`；候选与训练截止：`../state/plan.json`。',
            '未成交及原因见各仓 orders.csv；盘口原始快照见 quote_audit/。']
    lines+=['','## 分时段执行记录','']
    for key,record in sorted(ledger.get('sessions',{}).items())[-20:]:
        lines.append(f"- {key}: {record['status']}；{record.get('message','')} ({record.get('at','')})")
    (ROOT/'reports/latest.md').write_text('\n'.join(lines))
