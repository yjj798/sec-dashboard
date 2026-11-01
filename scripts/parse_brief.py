# -*- coding: utf-8 -*-
"""
finbrief 简报结构化解析器 —— 纯规则，不调任何模型
=================================================
输入：jixiangying/finance-auto-gen 的 reports/YYYY-MM-DD-{morning,evening}.md
输出：结构化 JSON（指数点位 / 成交额 / 涨停与连板个股 / 未来事件日历 / 情绪词频）

设计立场（重要）：
    这些 md 本身是 AI 写的散文。程序**读不懂因果**，但能可靠抽出三类硬事实：
      ① 带单位、带千分位的数字（点位/涨跌幅/成交额）
      ② 明确的日期 + 事件名（"11月2日（周一）上午：…座谈会"）
      ③ 股票名 + N连板/涨停 的搭配
    这三类足够做交叉校验与事件驱动排程，不需要理解"政策底"这种判断。
"""
import json
import re
from collections import Counter
from datetime import datetime

# ---------------- 指数名归一 ----------------
INDEX_ALIAS = {
    "上证指数": ["上证指数", "上证综指", "沪指", "SHCOMP"],
    "深证成指": ["深证成指", "深成指", "成指"],
    "创业板指": ["创业板指", "创业板指数"],
    "沪深300": ["沪深300", "沪深 300", "CSI 300", "CSI300"],
    "科创50": ["科创50", "科创 50", "科创板50"],
    "恒生指数": ["恒生指数", "恒指", "HSI"],
    "恒生科技": ["恒生科技指数", "恒生科技", "HSTECH"],
    "道琼斯": ["道指", "道琼斯"],
    "标普500": ["标普500", "标普 500"],
    "纳斯达克": ["纳指", "纳斯达克"],
}

NUM = r"(-?\d[\d,]*\.?\d*)"


def _f(s):
    try:
        return float(str(s).replace(",", ""))
    except Exception:
        return None


def _clean(md):
    """去掉图片、HTML 标签、引用前缀，保留正文。"""
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", md)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"^\s*>\s?", "", s, flags=re.M)
    return s


def parse_indices(text):
    """指数点位：必须「指数名 → 其后 40 字内出现 N点」就近配对。

    反面教训（第一版踩过）：按句切、取句内第一个点位，会把
    「上证指数报3,599.02点…深证成指报10,833.78点」整句都配成 3,599.02；
    标题里「恒指劲升1.46%…沪指险失3600点」会把涨幅 1.46 配给上证。
    ⇒ 必须按别名出现的位置就近取，且点位后紧跟的 % 才算它的涨跌幅。
    """
    out = {}
    BAD_PRE = ("失守", "险失", "跌破", "站上", "收复", "逼近", "关口", "整数关")
    for canon, aliases in INDEX_ALIAS.items():
        cands = []
        for a in aliases:
            for m in re.finditer(re.escape(a), text):
                window = text[m.end():m.end() + 40]
                pm = re.search(NUM + r"\s*点", window)
                if not pm:
                    continue
                close = _f(pm.group(1))
                if close is None or close < 100:
                    continue
                # 「沪指险失3600点」不是收盘价，是关口描述
                pre = window[max(0, pm.start() - 6):pm.start()]
                if any(b in pre for b in BAD_PRE):
                    continue
                after = window[pm.end():pm.end() + 20]
                # 中间夹了别的指数名 ⇒ 这个 % 不是它的，宁可留空
                if any(x in after for k2, al in INDEX_ALIAS.items() if k2 != canon
                       for x in al):
                    pct = None
                else:
                    pct = re.search(NUM + r"\s*%", after)
                if pct is None:
                    # 中文语序二：「收跌1.44%报3,599.02点」——百分比在点位前
                    before = window[max(0, pm.start() - 22):pm.start()]
                    pb = re.search(NUM + r"\s*%", before)
                    if pb:
                        v = _f(pb.group(1))
                        if re.search(r"(跌|回落|下滑|下挫)", before[-10:]):
                            v = -abs(v)
                        elif re.search(r"(涨|升|上行)", before[-10:]):
                            v = abs(v)
                        pct = type("X", (), {"group": staticmethod(lambda i: v)})()
                cands.append({
                    "close": close,
                    "pct": _f(pct.group(1)) if pct else None,
                    "src": (text[max(0, m.start() - 10):m.end() + 30]).strip(),
                })
        if cands:
            # 带涨跌幅的优先，其次取数字更"像指数"（>=500）的
            cands.sort(key=lambda x: (x["pct"] is not None, x["close"] >= 500), reverse=True)
            out[canon] = cands[0]
    return out


def parse_amount(text):
    """两市成交额（亿元）。注意千分位与「万元/万亿」单位混用。"""
    r = {}
    m = re.search(r"成交(?:额)?(?:达|约|为)?\s*" + NUM + r"\s*亿元", text)
    if m:
        r["hs_amt_yi"] = _f(m.group(1))
    m = re.search(r"较(?:昨日|前一交易日)[^。]{0,12}放量\s*" + NUM + r"\s*亿元", text)
    if m:
        r["delta_yi"] = _f(m.group(1))
    m = re.search(r"(?:昨日|前一交易日)[（(]?" + NUM + r"\s*亿元", text)
    if m:
        r["prev_yi"] = _f(m.group(1))
    return r


def parse_boards(text):
    """个股 + N连板 的搭配。先剥 markdown 加粗，否则会抽出 '**' 当股票名。"""
    t = text.replace("*", "")
    t = re.sub(r"(?:强势晋级|晋级|豪取|斩获|录得|收获|封死|斩获)", "  ", t)
    hits = {}
    for m in re.finditer(r"([\u4e00-\u9fa5A-Za-z]{2,6})\s*(?:豪取|斩获|录得|收获)?\s*"
                         r"(\d+)\s*连板", t):
        name = m.group(1)
        # 去掉粘在名字前的动词
        for pre in ("豪取", "斩获", "录得", "收获", "个股", "龙头"):
            if name.startswith(pre) and len(name) > len(pre) + 1:
                name = name[len(pre):]
        hits[name] = max(hits.get(name, 0), int(m.group(2)))
    return [{"name": k, "boards": v} for k, v in
            sorted(hits.items(), key=lambda x: -x[1])]


DATE_PAT = re.compile(r"(\d{1,2})月(\d{1,2})日")


def parse_events(text, base_date):
    """事件日历：只认「行首即日期」的条目，如
         *   **11月2日（周一）｜三大国内重磅节点同日共振**
    正文里随口提到的日期（"10月29日收盘 3,651.43点"）一律不要——
    第一版按「行内出现日期」抓，26 条里 20 条是论述噪声。
    """
    out = []
    y = base_date.year
    pat = re.compile(r"^\s*[*\-+]\s*\**\s*(\d{1,2})月(\d{1,2})日")
    for line in text.split("\n"):
        m = pat.match(line)
        if not m:
            continue
        mm, dd = int(m.group(1)), int(m.group(2))
        yy = y + (1 if (base_date.month == 12 and mm == 1) else 0)
        try:
            d = datetime(yy, mm, dd).date().isoformat()
        except ValueError:
            continue
        tail = line[m.end():]
        tail = re.sub(r"[（(][^）)]*[）)]", "", tail)     # 去星期/备注
        tail = re.sub(r"^[｜|、:：\s*]+", "", tail)
        title = re.sub(r"\*+", "", tail)
        title = re.split(r"[：:]", title)[0][:40].strip()
        if not title:
            continue
        out.append({"date": d, "title": title})
    seen, uniq = set(), []
    for e in out:
        k = (e["date"], e["title"][:10])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)
    return uniq


BULL = ["利好", "超预期", "回暖", "爆发", "大涨", "劲升", "反攻", "突破", "创新高",
        "净流入", "扩内需", "涨停潮", "修复", "加码", "支撑"]
BEAR = ["利空", "不及预期", "回落", "下跌", "调整", "净流出", "新低", "失守", "抛压",
        "重挫", "风险", "降温", "缩量", "承压"]


def parse_tone(text):
    c = Counter()
    for w in BULL:
        c["bull"] += text.count(w)
    for w in BEAR:
        c["bear"] += text.count(w)
    tot = c["bull"] + c["bear"]
    return {"bull": c["bull"], "bear": c["bear"],
            "skew": round((c["bull"] - c["bear"]) / tot * 100, 1) if tot else 0.0}


def parse(md, fname):
    """fname 形如 2026-10-30-evening.md"""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})-(morning|evening)", fname)
    base = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    body = _clean(md)
    head = body[:1200]                       # 摘要区，数字最密
    return {
        "date": base.date().isoformat(),
        "session": m.group(4),
        "title": (md.split("\n")[0] or "").lstrip("# ").strip()[:60],
        "indices": parse_indices(body),
        "amount": parse_amount(head),
        "boards": parse_boards(body),
        "events": parse_events(md, base),
        "tone": parse_tone(head),
        "chars": len(md),
    }


if __name__ == "__main__":
    import glob
    import os
    import sys
    res = []
    for p in sorted(glob.glob("finbrief/*.md")):
        r = parse(open(p, encoding="utf-8").read(), os.path.basename(p))
        res.append(r)
        print("=" * 70)
        print(r["date"], r["session"], "|", r["title"])
        print("  指数:", json.dumps(r["indices"], ensure_ascii=False)[:300])
        print("  成交:", r["amount"], " 连板:", r["boards"][:5])
        print("  事件:", len(r["events"]), "条 →", [f"{e['date']} {e['title'][:16]}"
                                                for e in r["events"][:6]])
        print("  情绪词: ", r["tone"])
    if len(sys.argv) > 1 and sys.argv[1] == "--json":
        json.dump(res, open("finbrief_parsed.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("\n已写 finbrief_parsed.json")
