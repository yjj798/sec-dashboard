# -*- coding: utf-8 -*-
"""抓取 finance-auto-gen 最近 N 天早晚报，用纯规则抽成结构化摘要。
不调用任何模型：只抽「指数点位 / 成交额 / 连板股 / 未来事件日历 / 情绪词频」。
输出 data/brief.json + data/brief.html
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_brief import parse  # noqa: E402

RAW = ("https://raw.githubusercontent.com/jixiangying/finance-auto-gen/"
       "main/reports/{d}-{s}.md")
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch(day, session):
    url = RAW.format(d=day, s=session)
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return None
        # 必须自己按 utf-8 解码：raw.githubusercontent 的响应头不带 charset，
        # requests 会退回 ISO-8859-1，中文全成乱码，正则一条也匹配不上。
        txt = r.content.decode("utf-8", errors="ignore")
        return txt if len(txt) > 500 else None
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=4)
    ap.add_argument("--data-dir", default=os.environ.get("SEC_DATA_DIR") or "data")
    a = ap.parse_args()
    os.makedirs(a.data_dir, exist_ok=True)

    today = date.today()
    got = []
    for i in range(a.days + 2):
        d = (today - timedelta(days=i)).isoformat()
        for s in ("evening", "morning"):
            md = fetch(d, s)
            if md:
                got.append(parse(md, f"{d}-{s}.md"))
    got.sort(key=lambda x: (x["date"], x["session"]))
    print(f"抓到 {len(got)} 篇简报")

    # 合并指数：多篇投票，取出现次数最多的收盘值（平票取最新）。
    # 单篇正文里的错配（如把"失守3,600点关口"当收盘价）会被多数票压掉。
    from collections import Counter
    votes = {}
    for r in got:
        for k, v in r["indices"].items():
            if not v.get("close"):
                continue
            votes.setdefault(k, []).append((round(v["close"], 2), v.get("pct"), r["date"]))
    idx = {}
    for k, cand in votes.items():
        cnt = Counter(c[0] for c in cand)
        top = cnt.most_common(1)[0][0]
        pick = [c for c in cand if c[0] == top]
        pick.sort(key=lambda x: (x[1] is not None, x[2]), reverse=True)
        idx[k] = {"close": pick[0][0], "pct": pick[0][1], "_date": pick[0][2]}
    # 未来事件：只保留 >= 今天
    ev, seen = [], set()
    for r in got:
        for e in r["events"]:
            if e["date"] < today.isoformat():
                continue
            k = (e["date"], e["title"][:8])
            if k in seen:
                continue
            seen.add(k)
            ev.append(e)
    ev.sort(key=lambda x: x["date"])

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "reports": len(got),
        "latest": got[-1]["date"] + " " + got[-1]["session"] if got else None,
        "indices": {k: {"close": v["close"], "pct": v["pct"], "as_of": v["_date"]}
                    for k, v in idx.items()},
        "events": ev[:20],
        "tone": got[-1]["tone"] if got else {},
        "source": "jixiangying/finance-auto-gen (AI 生成，仅作叙事参考)",
    }
    json.dump(out, open(os.path.join(a.data_dir, "brief.json"), "w",
                        encoding="utf-8"), ensure_ascii=False, indent=1)

    rows = "".join(
        f"<tr><td>{e['date']}</td><td>{e['title']}</td></tr>" for e in ev[:20])
    idx_rows = "".join(
        f"<tr><td>{k}</td><td>{v['close']:.2f}</td>"
        f"<td class=\"{'red' if (v['pct'] or 0) > 0 else 'green'}\">"
        f"{'' if v['pct'] is None else '%+.2f%%' % v['pct']}</td>"
        f"<td>{v['as_of']}</td></tr>" for k, v in out["indices"].items())
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>简报摘要 · 事件日历</title><style>
body{{background:#12161c;color:#e6e9ee;font:14px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
max-width:860px;margin:0 auto;padding:26px 18px 60px}}
h1{{font-size:20px}}h2{{font-size:14px;color:#8b95a3;margin:22px 0 10px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:7px 8px;border-bottom:1px solid #2a313c;text-align:left}}
th{{color:#8b95a3;font-weight:500;font-size:12px}}
.red{{color:#f2555a}}.green{{color:#2fbf71}}
.note{{color:#8b95a3;font-size:12px;margin-top:14px}}</style></head><body>
<h1>简报结构化摘要</h1>
<div class="note">来源 finance-auto-gen（AI 生成）。本页数字由纯正则抽取，未经模型改写；
因果判断不采信，只用事件日历与点位做交叉校验。生成于 {out['generated_at']}，共 {len(got)} 篇。</div>
<h2>指数基准（取最新一篇的收盘值）</h2><table><thead><tr><th>指数</th><th>收盘</th><th>涨跌</th><th>数据日期</th></tr></thead>
<tbody>{idx_rows or '<tr><td colspan=4>无</td></tr>'}</tbody></table>
<h2>未来事件日历（程序抽取，按日期排序）</h2><table><thead><tr><th>日期</th><th>事件</th></tr></thead>
<tbody>{rows or '<tr><td colspan=2>无</td></tr>'}</tbody></table>
<div class="note">注意：这些条目是从简报正文里按「行首日期」规则抽的，标题被截断到 40 字，
只用于排期提醒，不代表完整表述。</div>
</body></html>"""
    open(os.path.join(a.data_dir, "brief.html"), "w", encoding="utf-8").write(html)
    print(f"事件 {len(ev)} 条，指数 {len(idx)} 个 → brief.json / brief.html")


if __name__ == "__main__":
    main()
