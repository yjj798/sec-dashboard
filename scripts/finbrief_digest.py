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


def fetch(day, session, retry=2):
    """抓一篇简报。必须重试：raw.githubusercontent 会偶发超时/半包，
    一旦最新那篇没抓到，整个「事件日历」就会退化成旧篇的过期日期（表现为 events=0）。"""
    url = RAW.format(d=day, s=session)
    for i in range(retry + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 404:      # 真没有这篇，别浪费时间重试
                return None
            if r.status_code != 200:
                raise RuntimeError("HTTP %d" % r.status_code)
            # 必须自己按 utf-8 解码：raw.githubusercontent 的响应头不带 charset，
            # requests 会退回 ISO-8859-1，中文全成乱码，正则一条也匹配不上。
            txt = r.content.decode("utf-8", errors="ignore")
            if len(txt) > 500:
                return txt
            raise RuntimeError("body too short: %d" % len(txt))
        except Exception as e:
            if i >= retry:
                print("    fetch %s-%s 失败: %s %s" % (day, session, type(e).__name__, e))
                return None
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

    # 指数基准：以「最新一篇晚报」为准，旧篇只补它缺的收盘值。
    # 不能用多篇投票 —— 不同交易日的收盘价本来就不同，"出现次数多"没有正确性含义。
    # 实测反例：投票把 10-27 的 3648.30 选成上证，因为各值都只出现一次时，
    # Counter.most_common 返回的是最先插入的那篇（最早的），正好是最旧的值。
    primary = None
    for r in reversed(got):                       # got 已按 日期+场次 升序
        if r["session"] == "evening" and len(r["indices"]) >= 3:
            primary = r                            # 晚报才有收盘数据
            break
    if primary is None and got:
        primary = got[-1]

    idx = {}
    if primary is not None:
        for k, v in primary["indices"].items():
            if v.get("close"):
                idx[k] = {"close": v["close"], "pct": v.get("pct"), "_date": primary["date"]}
    # 最新篇没给出的指数，从更早的篇补收盘值；但**不给涨跌幅**——
    # 旧篇的 pct 属于别的交易日，套到最新收盘上是张冠李戴，宁可留空。
    for r in reversed(got):
        if primary is not None and r is primary:
            continue
        for k, v in r["indices"].items():
            if v.get("close") and k not in idx:
                idx[k] = {"close": v["close"], "pct": None, "_date": r["date"]}

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
    if not ev:
        _hint = (got[-1]["date"] + " " + got[-1]["session"]) if got else "无（全部抓取失败）"
        print("  ! 事件日历为空：抓到的最新篇是 %s，可能是抓取失败或该篇没有未来事件" % _hint)

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "reports": len(got),
        "latest": (primary["date"] + " " + primary["session"]) if primary else None,
        "reports_seen": [r["date"] + " " + r["session"] for r in got],
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
        f"<td class=\"{'' if v['pct'] is None else ('red' if v['pct'] > 0 else 'green')}\">"
        f"{'' if v['pct'] is None else '%+.2f%%' % v['pct']}</td>"
        f"<td>{v['as_of']}</td></tr>" for k, v in out["indices"].items())
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>简报摘要 · 事件日历</title><style>
:root{{--bg:#12161c;--card:#1a1f27;--line:#2a313c;--fg:#e6e9ee;--muted:#8b95a3;--red:#f2555a;--green:#2fbf71}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
.nav{{position:sticky;top:0;z-index:9;background:rgba(18,22,28,.94);
border-bottom:1px solid var(--line);backdrop-filter:blur(8px)}}
.navin{{max-width:960px;margin:0 auto;padding:0 18px;height:50px;display:flex;align-items:center;gap:18px}}
.brand{{font-size:13.5px;font-weight:600;letter-spacing:.4px;color:var(--fg);text-decoration:none;white-space:nowrap}}
.brand i{{font-style:normal;color:var(--muted);font-weight:400;font-size:12px;margin-left:7px}}
.tabs{{display:flex;gap:2px;margin-left:auto}}
.tabs a{{font-size:13px;color:var(--muted);text-decoration:none;padding:7px 13px;border-radius:7px;line-height:1.3}}
.tabs a:hover{{color:var(--fg);background:#222a34}}
.tabs a.on{{color:#fff;background:#2b3542;box-shadow:inset 0 -2px 0 #5b9bd5}}
.wrap{{max-width:960px;margin:0 auto;padding:28px 18px 60px}}
h1{{font-size:21px;margin:0 0 6px}}
h2{{font-size:14px;margin:0 0 12px;color:var(--muted);font-weight:600}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left}}
th{{color:var(--muted);font-weight:500;font-size:12px}}
tr:last-child td{{border-bottom:0}}
.red{{color:var(--red)}}.green{{color:var(--green)}}
.note{{font-size:12px;color:var(--muted);margin-top:10px;line-height:1.7}}
.xlink{{margin:24px 0 0}}
.xlink a{{display:block;background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:14px 16px;text-decoration:none;color:var(--fg)}}
.xlink a:hover{{border-color:#3c4859;background:#1e242e}}
.xlink b{{font-size:13.5px;font-weight:600}}
.xlink span{{display:block;color:var(--muted);font-size:12px;margin-top:5px;line-height:1.6}}
@media(max-width:520px){{.brand i{{display:none}}.tabs a{{padding:7px 10px;font-size:12.5px}}}}
</style></head><body>
<nav class="nav"><div class="navin">
<a class="brand" href="./">A 股每日观察<i>sec-dashboard</i></a>
<div class="tabs"><a href="./">复盘</a><a class="on" href="brief.html">简报 · 事件日历</a></div>
</div></nav>
<div class="wrap">
<h1>简报摘要 · 事件日历</h1>
<div class="note" style="margin:0 0 20px">来源 finance-auto-gen（AI 生成）。本页数字由纯正则抽取，未经模型改写；
因果判断不采信，只用事件日历与点位做交叉校验。生成于 {out['generated_at']}，共 {len(got)} 篇。</div>
<div class="card"><h2>指数基准（取最新一篇的收盘值）</h2><table>
<thead><tr><th>指数</th><th>收盘</th><th>涨跌</th><th>数据日期</th></tr></thead>
<tbody>{idx_rows or '<tr><td colspan=4>无</td></tr>'}</tbody></table></div>
<div class="card"><h2>未来事件日历（程序抽取，按日期排序）</h2><table>
<thead><tr><th>日期</th><th>事件</th></tr></thead>
<tbody>{rows or '<tr><td colspan=2>无</td></tr>'}</tbody></table>
<div class="note">这些条目按简报正文「行首日期」规则抽取，标题截断到 40 字，只用于排期提醒，不代表完整表述。</div></div>
<div class="xlink"><a href="./"><b>← 回到每日复盘</b>
<span>情绪温度、封板率、首板晋级率、涨停驱动词 TOP20、连板梯队。
数据来自 55188 sec.php 公开接口 + 同花顺异动解读，逐日快照沉淀。</span></a></div>
<div class="note">本页为程序抽取的辅助信息，不是行情源，不构成投资建议。</div>
</div></body></html>"""
    open(os.path.join(a.data_dir, "brief.html"), "w", encoding="utf-8").write(html)
    print(f"事件 {len(ev)} 条，指数 {len(idx)} 个 → brief.json / brief.html")


if __name__ == "__main__":
    main()
