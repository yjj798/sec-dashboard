# -*- coding: utf-8 -*-
"""
55188 sec.php 每日复盘采集器（纯 Python 独立版）

用法:
    python sec_daily.py              # 采集 + 计算派生指标 + 存快照 + 生成 report.html
    python sec_daily.py --no-html    # 只存数据
    python sec_daily.py --show       # 采集并打印指标，不落盘

依赖: requests（pip install requests）

数据来源: https://www.55188.com/sec.php?action=<name>
    overview   今日市场全景（指数/涨跌分布/涨停梯队/热门板块/主力资金榜/概念资金流）
    qxzb       同花顺口径的涨跌与涨停统计（含晋级率原始字段）
    blocks     精选板块（站点自有 strength 评分）
    fry_themes 题材前瞻（trade_date 是下一交易日，注意未来函数）
    lhb_board  龙虎榜（榜单/个股/机构/席位）

落盘结构:
    data/snapshots/2026-10-30.json   当日完整快照
    data/series.json                 历史序列（派生指标逐日累积）
    data/report.html                 最新一日报表
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

try:
    import requests
except ImportError:
    sys.exit("缺少依赖: pip install requests")

BASE = "https://www.55188.com/sec.php?action="
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.55188.com/sec.php",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("SEC_DATA_DIR") or os.path.join(HERE, "data")
CST = timezone(timedelta(hours=8))

try:
    import reasons                       # 同花顺涨停驱动词（可选模块，缺失时主流程照跑）
except ImportError:
    reasons = None


# ---------------------------------------------------------------- 工具
def num(v, d=0.0):
    """任何脏值都退化成默认值，绝不抛异常——接口字段缺失是常态。"""
    try:
        if v is None or v == "":
            return d
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return d
        return f
    except (TypeError, ValueError):
        return d


def rnd(v, d=0):
    p = 10 ** d
    return round(v * p) / p


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def yi(v):
    if not v:
        return "—"
    a = abs(v)
    if a >= 1e12:
        return "%.2f 万亿" % (v / 1e12)
    if a >= 1e8:
        return "%.0f 亿" % (v / 1e8)
    if a >= 1e4:
        return "%.0f 万" % (v / 1e4)
    return "%.0f" % v


def hhmm(ts):
    """首封时间：接口给的是秒级或毫秒级时间戳，转成 HH:MM（东八区）。"""
    t = num(ts)
    if not t:
        return ""
    if t > 1e11:
        t /= 1000
    return datetime.fromtimestamp(t, CST).strftime("%H:%M")


def mins_of(t):
    if not t or len(t) < 5:
        return 0
    return int(t[:2]) * 60 + int(t[3:5])


# ---------------------------------------------------------------- 抓取
def fetch(action, extra=""):
    """单接口抓取。失败重试一次——源站偶发 502，但整体很稳（CF 出口约 3 秒抓完 5 个）。"""
    url = BASE + action + extra
    last = None
    for i in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            if r.status_code != 200:
                raise RuntimeError("HTTP %d" % r.status_code)
            d = r.json()
            if isinstance(d, dict) and d.get("status") not in (None, "ok"):
                raise RuntimeError("status=%s" % d.get("status"))
            return True, d
        except Exception as e:
            last = e
            if i == 0:
                time.sleep(1.2)
    return False, str(last)


def collect_offline(raw_dir):
    """从本地已抓好的 JSON 复算，用于回归测试和排查（不联网）。"""
    out = []
    for name in ("overview", "blocks", "fry_themes", "lhb_board", "qxzb"):
        p = os.path.join(raw_dir, raw_dir and (name + ".json") or "")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                out.append((True, json.load(f)))
        else:
            out.append((False, "missing %s" % name))
    return out


def collect(raw_dir=None):
    if raw_dir:
        jobs = collect_offline(raw_dir)
    else:
        jobs = [
            fetch("overview"),
            fetch("blocks"),
            fetch("fry_themes", "&days=3"),
            fetch("lhb_board"),
            fetch("qxzb"),
        ]
    (ov_ok, ov), (bl_ok, bl), (fry_ok, fry), (lhb_ok, lhb), (qx_ok, qx) = jobs
    degraded = []
    for ok, tag, name in ((ov_ok, "overview", ov), (bl_ok, "blocks", bl),
                          (fry_ok, "fry_themes", fry), (lhb_ok, "lhb_board", lhb),
                          (qx_ok, "qxzb", qx)):
        if not ok:
            degraded.append("%s:%s" % (tag, name))

    O = ov if ov_ok else {}
    B = bl if bl_ok else {}
    F = fry if fry_ok else {}
    L = lhb if lhb_ok else {}
    Q = qx if qx_ok else {}

    o = O.get("overview") or {}
    met = O.get("metrics") or {}
    qb = ((Q.get("payload") or {}).get("breadth")) or {}
    ql = ((Q.get("payload") or {}).get("limit")) or {}

    def pick(a, b):
        return num(a if a is not None else b)

    # 涨停梯队：这里带 plates[].reason（涨停原因）和首封时间，是选股唯一可用的原始字段
    ladder = []
    for x in (O.get("limit_ladder") or []):
        stocks = []
        for s in (x.get("stocks") or [])[:60]:
            code = str(s.get("code") or s.get("symbol") or "")
            for p in ("sh", "sz", "bj", "SH", "SZ", "BJ"):
                if code.startswith(p):
                    code = code[len(p):]
                    break
            stocks.append({
                "code": code,
                "name": s.get("name") or "",
                "first": num(s.get("first_limit_up")),
                "last": num(s.get("last_limit_up")),
                "plates": [{"name": p.get("name") or "",
                            "reason": str(p.get("reason") or "")[:70]}
                           for p in (s.get("plates") or [])[:3]],
            })
        ladder.append({"board": int(num(x.get("board"))),
                       "count": int(num(x.get("count"))),
                       "stocks": stocks})

    hot_sectors = []
    for x in (O.get("hot_sectors") or [])[:15]:
        hot_sectors.append({
            "name": x.get("name") or "",
            "limit_up_count": int(num(x.get("limit_up_count"))),
            "max_board": int(num(x.get("max_board"))),
            "stock_count": len(x.get("stocks") or []),
        })

    mf = O.get("main_force") or {}
    main_force = []
    for x in (mf.get("all") or [])[:60]:
        main_force.append({
            "code": str(x.get("code") or ""), "name": x.get("name") or "",
            "rank": int(num(x.get("rank"))), "price": num(x.get("price")),
            "pct": num(x.get("change_pct")), "mf_pct": num(x.get("main_force_pct")),
            "sector": x.get("sector") or "",
        })

    sector_flow = []
    for x in (O.get("themes") or [])[:15]:
        sector_flow.append({
            "name": x.get("name") or "", "pct": num(x.get("change_pct")),
            "net": num(x.get("main_force_net_inflow")),
            "up": int(num(x.get("up_count"))), "down": int(num(x.get("down_count"))),
            "leader": x.get("leader_name") or "",
            "leader_pct": num(x.get("leader_change_pct")),
        })

    blocks = []
    for x in (B.get("rows") or [])[:15]:
        blocks.append({"id": x.get("id"), "name": x.get("name") or "",
                       "inc": num(x.get("inc")), "strength": num(x.get("strength")),
                       "main_net": num(x.get("main_net"))})

    days = (F.get("payload") or {}).get("days") or []
    d0 = days[0] if days else {}
    themes = {"date": d0.get("date") or (F.get("payload") or {}).get("trade_date") or "",
              "items": []}
    for x in (d0.get("items") or [])[:10]:
        themes["items"].append({
            "code": x.get("code") or "", "name": x.get("name") or "",
            "rank": int(num(x.get("rank"))), "title": x.get("title") or "",
            "summary": (x.get("summary") or "")[:160],
            "change_pct": num(x.get("change_pct")),
            "cum_pct": num(x.get("cumulative_change_pct")),
            "limit_up_count": int(num(x.get("limit_up_count"))),
            "is_hot": bool(x.get("is_hot")), "is_new": bool(x.get("is_new")),
        })

    p = L.get("payload") or {}
    st = p.get("stats") or {}
    lhb = {"date": p.get("date") or "",
           "count": int(num(st.get("count"))), "org_count": int(num(st.get("org_count"))),
           "seat_count": int(num(st.get("seat_count"))),
           "up": int(num(st.get("up"))), "down": int(num(st.get("down"))),
           "net_total": num(st.get("net_total"))}
    lhb_stocks = []
    for x in (p.get("stocks") or [])[:15]:
        lhb_stocks.append({"code": x.get("code"), "name": x.get("name"),
                           "pct": num(x.get("pct")), "net": num(x.get("net")),
                           "turnover": num(x.get("turnover")),
                           "board": x.get("board") or "", "org": int(num(x.get("org")))})
    lhb["stocks"] = lhb_stocks

    snap = {
        "trade_date": O.get("trade_date") or (Q.get("payload") or {}).get("trade_date"),
        "as_of": O.get("as_of"),
        "collected_at": datetime.now(CST).isoformat(timespec="seconds"),
        "breadth": {
            "up": int(pick(o.get("up_count"), qb.get("up"))),
            "down": int(pick(o.get("down_count"), qb.get("down"))),
            "flat": int(pick(o.get("flat_count"), qb.get("flat"))),
            "limit_up": int(pick(o.get("limit_up_count"), qb.get("limit_up"))),
            "limit_down": int(pick(o.get("limit_down_count"), qb.get("limit_down"))),
            "broken": int(num(o.get("broken_count"))),
            "limit_down_open": int(num((met.get("limit_down_open_count") or {}).get("value"))),
            "suspended": int(num(o.get("suspended_count"))),
            "amount": num(o.get("estimated_amount")),
            "prev_amount": num(o.get("previous_amount")),
            "amount_change": num(o.get("amount_change")),
            "coverage": num(o.get("coverage_rate")),
        },
        "sentiment": {
            "seal_rate": num((met.get("seal_rate") or {}).get("value")),
            "prev_seal_rate": num((met.get("previous_seal_rate") or {}).get("value")),
            "max_board": int(pick(o.get("max_board"), (met.get("max_board") or {}).get("value"))),
            "sprint_rate": num(ql.get("sprint_rate")),
            "continue_count": int(num(ql.get("continue_count"))),
            "prev_limit_up": int(num(ql.get("prev_limit_up"))),
            "once_limit_up": int(num(ql.get("once_limit_up"))),
            "broken_count": int(num(ql.get("broken_count"))),
            "top_board_stock": ql.get("top_board_stock") or "",
            "top_board_count": int(num(ql.get("top_board_count"))),
            "main_net": num(qb.get("main_net")),
            "qx_seal_rate": num(ql.get("seal_rate")),
        },
        "ladder": ladder,
        "hot_sectors": hot_sectors,
        "main_force": main_force,
        "sector_flow": sector_flow,
        "blocks": blocks,
        "themes": themes,
        "lhb": lhb,
        "sources": {
            "overview": O.get("as_of") or "",
            "snapshot_id": O.get("snapshot_id") or "",
            "qxzb": (Q.get("payload") or {}).get("fetched_at") or "",
            "fry": F.get("checked_at") or "",
            "lhb": L.get("checked_at") or "",
            "blocks": B.get("at") or "",
        },
        "degraded": degraded,
    }
    return snap


# ---------------------------------------------------------------- 派生指标
def rank_pct(hist, v):
    if not hist:
        return None
    return sum(1 for x in hist if x <= v) / len(hist) * 100


def score_abs(v, lo, hi):
    return clamp((v - lo) / (hi - lo) * 100, 0, 100)


LEVELS = [(75, "火热"), (60, "偏暖"), (45, "中性"), (30, "偏冷"), (0, "冰点")]


def derive(snap, hist, prev_row=None):
    """hist 为历史序列（不含当日）。样本 < 20 天用经验阈值，>= 20 天切历史分位。

    首板晋级率 = 今日连板家数 / 昨日涨停家数。没有昨日数据时为 None，
    不做任何猜测——这一项缺失会让温度少一个 25% 权重的分项，页面需明示。
    """
    b, s = snap["breadth"], snap["sentiment"]
    total = b["up"] + b["down"] + b["flat"]
    up_ratio = b["up"] / total * 100 if total else 0.0

    prev = prev_row
    promo = None
    if prev and num(prev.get("limit_up")) > 0 and s["continue_count"]:
        promo = s["continue_count"] / num(prev["limit_up"]) * 100

    denom = b["limit_up"] + b["broken"]
    broken_rate = b["broken"] / denom * 100 if denom else 0.0
    seal_rate = s["seal_rate"] if s["seal_rate"] > 0 else (100 - broken_rate)

    seal_h = [x["seal_rate"] for x in hist if num(x.get("seal_rate")) > 0]
    promo_h = [x["promotion"] for x in hist if x.get("promotion") is not None]
    lu_h = [x["limit_up"] for x in hist if num(x.get("limit_up")) > 0]
    h_h = [x["height"] for x in hist]
    ur_h = [x["up_ratio"] for x in hist]
    use_rank = len(hist) >= 20

    def pick(cur, arr, lo, hi):
        if use_rank and len(arr) >= 10:
            r = rank_pct(arr, cur)
            if r is not None:
                return r, "rank"
        return score_abs(cur, lo, hi), "abs"

    items = []
    sc, _ = pick(seal_rate, seal_h, 40, 75)
    items.append({"key": "seal", "name": "封板率", "value": seal_rate, "unit": "%",
                  "score": sc, "weight": 0.25})
    if promo is not None:
        sc, _ = pick(promo, promo_h, 10, 50)
        items.append({"key": "promo", "name": "首板晋级率", "value": promo, "unit": "%",
                      "score": sc, "weight": 0.25})
    sc, _ = pick(b["limit_up"], lu_h, 20, 100)
    items.append({"key": "lu", "name": "涨停家数", "value": b["limit_up"], "unit": "",
                  "score": sc, "weight": 0.20})
    sc, _ = pick(s["max_board"], h_h, 2, 8)
    items.append({"key": "height", "name": "连板高度", "value": s["max_board"], "unit": "板",
                  "score": sc, "weight": 0.15})
    sc, _ = pick(up_ratio, ur_h, 15, 65)
    items.append({"key": "breadth", "name": "上涨占比", "value": up_ratio, "unit": "%",
                  "score": sc, "weight": 0.15})

    wsum = sum(i["weight"] for i in items)
    temp = sum(i["score"] * i["weight"] for i in items) / wsum if wsum else 0.0

    level = "中性"
    for th, name in LEVELS:
        if temp >= th:
            level = name
            break

    return {
        "up_ratio": up_ratio,
        "promotion": promo,
        "broken_rate": broken_rate,
        "seal_rate": seal_rate,
        "temp": {"score": temp, "level": level,
                 "mode": "rank" if use_rank else "abs", "items": items},
        "sample_days": len(hist) + 1,
    }


def regime_of(temp):
    if temp >= 75:
        return {"key": "hot", "label": "过热", "pos": "3-5 成"}
    if temp >= 60:
        return {"key": "warm", "label": "偏暖", "pos": "5-7 成"}
    if temp >= 45:
        return {"key": "mid", "label": "中性", "pos": "3-5 成"}
    if temp >= 30:
        return {"key": "cold", "label": "偏冷", "pos": "1-3 成"}
    return {"key": "ice", "label": "冰点", "pos": "0-1 成"}


def to_row(snap, dv):
    # 字段与 JS 版 toSeriesRow 对齐，两版共用同一个 series.json 不会打架
    return {
        "date": snap["trade_date"],
        "up": snap["breadth"]["up"],
        "down": snap["breadth"]["down"],
        "temp": rnd(dv["temp"]["score"], 1),
        "level": dv["temp"]["level"],
        "seal_rate": rnd(dv["seal_rate"], 2),
        "promotion": None if dv["promotion"] is None else rnd(dv["promotion"], 2),
        "limit_up": snap["breadth"]["limit_up"],
        "limit_down": snap["breadth"]["limit_down"],
        "broken": snap["breadth"]["broken"],
        "height": snap["sentiment"]["max_board"],
        "up_ratio": rnd(dv["up_ratio"], 2),
        "amount": snap["breadth"]["amount"],
        "lhb_net": snap["lhb"]["net_total"],
        "mode": dv["temp"]["mode"],
    }


# ---------------------------------------------------------------- 落盘
def save(snap, dv, series):
    os.makedirs(os.path.join(DATA, "snapshots"), exist_ok=True)
    day = snap["trade_date"] or datetime.now(CST).strftime("%Y-%m-%d")
    with open(os.path.join(DATA, "snapshots", day + ".json"), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    with open(os.path.join(DATA, "series.json"), "w", encoding="utf-8") as f:
        json.dump(series, f, ensure_ascii=False, indent=1)
    return day


def load_series():
    p = os.path.join(DATA, "series.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return []


def brief(snap, dv):
    t = dv["temp"]
    b, s = snap["breadth"], snap["sentiment"]
    print("交易日 %s（数据时点 %s）" % (snap["trade_date"], snap["as_of"]))
    print("  情绪温度  %.1f · %s   口径=%s  样本=%d 天"
          % (t["score"], t["level"], t["mode"], dv["sample_days"]))
    print("  涨/跌/平  %d / %d / %d    涨停 %d  跌停 %d  炸板 %d"
          % (b["up"], b["down"], b["flat"], b["limit_up"], b["limit_down"], b["broken"]))
    print("  封板率 %.2f%%   最高 %d 板 %s   成交 %s"
          % (dv["seal_rate"], s["max_board"], s["top_board_stock"], yi(b["amount"])))
    print("  首板晋级率 %s" % ("数据不足（缺昨日涨停数）" if dv["promotion"] is None
                          else "%.1f%%" % dv["promotion"]))
    for i in t["items"]:
        print("    - %-6s %8.2f%-2s  得分 %5.1f  权重 %.2f"
              % (i["name"], i["value"], i["unit"], i["score"], i["weight"]))
    print("  仓位建议 %s（%s）" % (regime_of(t["score"])["pos"], regime_of(t["score"])["label"]))
    sm = driver_summary(snap)
    if sm:
        c = sm["coverage"]
        print("  驱动词覆盖 %d/%d（%.0f%%）" % (c["hit_same_day"], c["pool"], c["rate"]))
        for d in sm["all"][:6]:
            tag = ("（泛）" if d["generic"]
                   else ("（暗线）" if (not d["in_sector_view"] and d["count"] >= 2) else ""))
            print("    %-14s %2d 只  最高 %d 板%s  %s"
                  % (d["keyword"], d["count"], d["max_board"], tag,
                     "、".join(s["name"] for s in d["stocks"][:4])))
    if snap["degraded"]:
        print("  ⚠ 降级接口: %s" % ", ".join(snap["degraded"]))


WIDE = {"业绩增长", "大消费", "其他", "ST股", "次新股", "预盈预增", "超跌反弹", "低价股",
        "破净股", "高送转", "参股金融", "融资融券", "机构重仓", "基金重仓", "QFII重仓",
        "社保重仓", "深股通", "沪股通", "MSCI中国", "标准普尔", "富时罗素", "股权转让",
        "举牌", "壳资源"}


def _esc(s):
    """HTML 转义：板块名/个股名可能带 & < > 等字符。"""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def driver_summary(snap):
    """把驱动词聚类压成三档：全量第一（含泛词）、具体第一、高度第一、暗线。

    分三档是因为它们回答三个不同问题：
      top_all     —— 当日涨停「最多被什么解释」（含业绩类，往往是普涨遮羞布）
      broad_spec  —— 抛开泛词后「最广的具体题材」（才是钱真的扎堆处）
      high        —— 「撑起最高标的那条线」（决定次日风向标归属）
      hidden      —— 55188 板块榜上根本没有的词（游资暗线，站外看不见）
    """
    ds = snap.get("drivers")
    cov = snap.get("driver_coverage")
    if not ds or not cov:
        return None
    spec = [d for d in ds if not d["generic"] and d["count"] >= 2]
    solid = [d for d in ds if not d["generic"]]      # 不限只数：最高标可能只有一只
    broad_spec = max(spec, key=lambda d: (d["count"], d["max_board"]), default=None)
    high = max(solid, key=lambda d: (d["max_board"], d["count"]), default=None)
    hidden = sorted([d for d in spec if not d["in_sector_view"]],
                    key=lambda d: (-d["count"], -d["max_board"]))[:3]
    return {"coverage": cov, "top_all": ds[0] if ds else None,
            "broad_spec": broad_spec, "high": high, "hidden": hidden, "all": ds}


def build_narrative(snap, dv):
    """当日结论：规则生成，每句可回溯到一个字段。与 JS 版 buildNarrative 同源同逻辑。

    设计约束：① 宁可少输出一条，也不写无数据支撑的判断；
              ② 板块名匹配不上资金流时必须声明，不能让人以为"没流出"。
    """
    b, s = snap["breadth"], snap["sentiment"]
    rg = regime_of(dv["temp"]["score"])
    out = []

    # ① 环境定调
    amt_pct = b["amount_change"] / b["prev_amount"] * 100 if b["prev_amount"] else 0.0
    v = ("温度 <b>%.0f 分</b>（%s），对应仓位 <b>%s</b>。成交 %s，较昨日 %+.1f%%。"
         % (dv["temp"]["score"], dv["temp"]["level"], rg["pos"], yi(b["amount"]), amt_pct))
    p = dv["promotion"]
    if p is not None:
        tag = ("接力顺畅，主线连板可以参与。" if p >= 40
               else "接力一般，只做首板，不接连板。" if p >= 25 else "打板就是亏，只低吸不追涨。")
        v += "首板晋级率 <b>%.1f%%</b>——%s" % (p, tag)
    else:
        v += "首板晋级率<b>暂缺</b>（需前一交易日涨停数），该项未参与加权，温度口径不完整。"
    out.append(("环境定调", v))

    # ② 主线判定
    eff = [x for x in snap["hot_sectors"] if x["name"] not in WIDE and x["limit_up_count"] >= 2]
    total_lu = sum(x["count"] for x in snap["ladder"]) or b["limit_up"]
    if eff:
        top = eff[:3]
        conc = sum(x["limit_up_count"] for x in top) / total_lu * 100 if total_lu else 0
        names = "、".join(
            "<b>%s</b>（%d 只涨停%s）" % (_esc(x["name"]), x["limit_up_count"],
                                      "，最高 %d 板" % x["max_board"] if x["max_board"] else "")
            for x in top)
        tone = ("集中度高，钱确实在一个方向，主线成立。" if conc >= 50
                else "集中度中等，有一到两条线可跟。" if conc >= 30
                else "集中度低，涨停是散的，这种普涨式分散通常不持续。")
        out.append(("主线判定", "<b>%d 个板块</b>有板块效应，前三：%s。三者合计占全场涨停 "
                            "<b>%.0f%%</b>——%s" % (len(eff), names, conc, tone)))
    else:
        out.append(("主线判定", "没有「2 只以上涨停且非泛标签」的板块，今日<b>无板块效应</b>。"
                            "全是个股行为，孤立涨停次日大概率不延续，不值得追。"))

    # ②' 驱动词口径：板块榜看不出「为什么涨」，这里看个股驱动词
    sm = driver_summary(snap)
    if sm:
        c, ta, bs, hi = sm["coverage"], sm["top_all"], sm["broad_spec"], sm["high"]
        hd = [x for x in (sm["hidden"] or []) if not (bs and x["keyword"] == bs["keyword"])]
        seg = ["按<b>个股驱动词</b>聚类（同花顺异动解读，命中 <b>%d/%d</b> 只涨停）："
               % (c["hit_same_day"], c["pool"])]
        bs_note = ("" if (not bs or bs["in_sector_view"])
                   else "（注意：这个词在 55188 板块榜里<b>根本没有</b>，只能从驱动词看得见）")
        if ta and ta["generic"] and bs:
            seg.append("被解释得最多的是业绩类「%s」<b>%d 只</b>，但这只是普涨的遮羞布——"
                       "抛开业绩/国资这类泛词，最广的具体题材是「<b>%s</b>」<b>%d 只</b>%s。"
                       % (_esc(ta["keyword"]), ta["count"], _esc(bs["keyword"]),
                          bs["count"], bs_note))
        elif bs:
            seg.append("最广的具体题材是「<b>%s</b>」<b>%d 只</b>（最高 %d 板）%s。"
                       % (_esc(bs["keyword"]), bs["count"], bs["max_board"], bs_note))
        if hi and bs and hi["keyword"] == bs["keyword"]:
            seg.append("最高标也在这条线上（<b>%d 板</b>），广度和高度合一，主升结构完整。"
                       % hi["max_board"])
        elif hi and hi["count"] == 1:
            seg.append("撑起最高标的是「<b>%s</b>」，但它<b>只有 1 只</b>——最高标靠个股自身逻辑"
                       "（并购/易主/中标一类），没有跟风票，这种高度<b>不可复制</b>，"
                       "别拿它当题材热度的证明。" % _esc(hi["keyword"]))
        elif hi:
            seg.append("撑起最高标的是「<b>%s</b>」（%d 板，%d 只），与最广的那条线"
                       "<b>不是同一题材</b>——广度线赚情绪、高度线赚溢价，两边不能混着做。"
                       % (_esc(hi["keyword"]), hi["max_board"], hi["count"]))
        if hd:
            seg.append("%s<b>板块榜上看不到</b>的线：%s——这是当日游资暗线，站内板块排名刷不出来。"
                       % ("另有一条" if len(hd) == 1 else "另有 %d 条" % len(hd),
                          "、".join("<b>%s</b>（%d 只，最高 %d 板）"
                                   % (_esc(x["keyword"]), x["count"], x["max_board"])
                                   for x in hd)))
        out.append(("驱动词口径", "".join(seg)))

    # ③ 资金与情绪是否同向
    flow = {x["name"]: x for x in snap["sector_flow"]}
    diverge, agree, unmatched = [], [], 0
    for x in eff[:8]:
        f = flow.get(x["name"])
        if not f or not f["net"]:
            unmatched += 1
            continue
        if f["net"] < 0:
            diverge.append("%s（涨停 %d 只，主力却净流出 %s）"
                           % (x["name"], x["limit_up_count"], yi(f["net"])))
        else:
            agree.append(x["name"])
    gap = ("（另有 %d 个板块未匹配到资金流数据，口径不同，不作判断）" % unmatched) if unmatched else ""
    if diverge:
        out.append(("资金背离", "<b>%s</b>。涨停是情绪推的，不是资金推的——这类板块次日容易直接断，"
                            "看的多做的少。%s" % (_esc("、".join(diverge)), gap)))
    elif agree:
        who = ("仅 <b>%s</b>" % _esc(agree[0]) if len(agree) == 1
               else "<b>%s</b>" % _esc("、".join(agree[:4])))
        out.append(("资金同向", "有板块效应的方向里，%s 主力净流入，情绪与资金同向，质量比单看涨停数高。%s"
                            % (who, gap)))
    elif unmatched:
        out.append(("资金验证", "有板块效应的方向<b>全部未匹配到资金流数据</b>，今日无法判断涨停是资金推动"
                            "还是情绪推动。这是口径限制，不是结论。"))

    # ④ 效应检验
    v = ("跌停 <b>%d</b> 只、炸板 <b>%d</b> 只，炸板率 %.1f%%。"
         % (b["limit_down"], b["broken"], dv["broken_rate"]))
    if b["limit_down"] > b["limit_up"]:
        v += "跌停数<b>超过</b>涨停数，亏钱效应大于赚钱效应，这个环境下买入是逆风。"
    elif dv["broken_rate"] >= 35:
        v += "炸板率偏高，封板不牢，追板的钱容易被埋。"
    else:
        v += "封板质量尚可，涨停含金量不差。"
    out.append(("效应检验", v))

    # ⑤ 高度风向标
    if s["max_board"] >= 3:
        tip = ("已在高位区。它一旦断板，同题材跟风股会同步杀，这个信号比任何分数都优先。"
               if s["max_board"] >= 5 else "高度未到极端，仍有空间，但它是全场情绪的锚，盯它。")
        out.append(("高度风向标", "最高 <b>%d 板 %s</b>——%s"
                                 % (s["max_board"], _esc(s["top_board_stock"] or "—"), tip)))

    # ⑥ 次日预案
    th = (snap["themes"]["items"] or [])[:3]
    if th:
        out.append(("次日预案", "题材前瞻前三：<b>%s</b>。判定标准只有一个——次日能否出现 2 只以上涨停，"
                            "能扩散才算题材成立，单只涨停只是个股行为。"
                            % _esc("、".join(x["name"] for x in th))))

    return out


def render(snap, dv, series):
    t, b = dv["temp"], snap["breadth"]
    rg = regime_of(t["score"])
    rows = "".join(
        "<tr><td>%s</td><td>%.2f%s</td><td>%d</td><td>%.1f</td></tr>"
        % (i["name"], i["value"], (" " + i["unit"]) if i["unit"] else "", i["weight"] * 100, i["score"])
        for i in t["items"])
    hist_rows = "".join(
        "<tr><td>%s</td><td>%.1f</td><td>%s</td><td>%.2f%%</td><td>%s</td><td>%d</td></tr>"
        % (x["date"], x["temp"], x["level"], x["seal_rate"],
           "—" if x["promotion"] is None else "%.1f%%" % x["promotion"], x["limit_up"])
        for x in reversed(series[-30:]))
    sec_rows = "".join(
        "<tr><td>%d. %s</td><td>%d</td><td>%d</td><td>%d</td></tr>"
        % (i + 1, x["name"], x["limit_up_count"], x["max_board"], x["stock_count"])
        for i, x in enumerate(snap["hot_sectors"]))
    th_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%.2f%%</td><td>%d</td></tr>"
        % (x["name"], x["title"][:40], x["cum_pct"], x["limit_up_count"])
        for x in snap["themes"]["items"][:8])

    # 驱动词表：标出「泛词」与「暗线」，这两类正是站内板块榜表达不出来的
    ds = snap.get("drivers") or []
    dcov = snap.get("driver_coverage") or {}
    drv_rows = "".join(
        "<tr><td>%s%s</td><td>%d</td><td>%d</td>"
        "<td style=\"text-align:left;color:var(--muted);font-size:12px\">%s</td></tr>"
        % (_esc(d["keyword"]),
           " <span style=\"color:#7d8794;font-size:11px\">泛</span>" if d["generic"]
           else (" <span style=\"color:#e8a04a;font-size:11px\">暗线</span>"
                 if (not d["in_sector_view"] and d["count"] >= 2) else ""),
           d["count"], d["max_board"],
           _esc("、".join(s["name"] for s in d["stocks"][:6])))
        for d in ds[:20])

    narr_rows = "".join(
        '<div style="display:flex;gap:10px;padding:11px 0;border-bottom:1px solid var(--line)">'
        '<div style="flex:0 0 20px;height:20px;border-radius:50%%;background:#242c37;color:#9aa6b6;'
        'font-size:11px;display:flex;align-items:center;justify-content:center;margin-top:2px">%d</div>'
        '<div><div style="font-size:12px;color:var(--muted);margin-bottom:3px">%s</div>'
        '<div style="font-size:13.5px;line-height:1.75">%s</div></div></div>'
        % (i, _esc(k), v)
        for i, (k, v) in enumerate(build_narrative(snap, dv), 1))

    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(date)s 市场复盘 · 情绪 %(temp).1f</title>
<style>
:root{--bg:#12161c;--card:#1a1f27;--line:#2a313c;--fg:#e6e9ee;--muted:#8b95a3;--red:#f2555a;--green:#2fbf71}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:28px 18px 60px}
h1{font-size:21px;margin:0 0 6px}h2{font-size:14px;margin:0 0 12px;color:var(--muted);font-weight:600}
.meta{color:var(--muted);font-size:12.5px;margin-bottom:22px}
.hero{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:16px}
.big{font-size:44px;font-weight:700;line-height:1;letter-spacing:-1px}
.row{display:flex;gap:26px;flex-wrap:wrap;margin-top:14px}
.row div{font-size:12.5px;color:var(--muted)}
.row b{display:block;font-size:17px;color:var(--fg);margin-top:2px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}
table{width:100%%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:500;font-size:12px}
tr:last-child td{border-bottom:0}
.red{color:var(--red)}.green{color:var(--green)}
.note{font-size:12px;color:var(--muted);margin-top:10px;line-height:1.7}
.nav{position:sticky;top:0;z-index:9;background:rgba(18,22,28,.94);
border-bottom:1px solid var(--line);backdrop-filter:blur(8px)}
.navin{max-width:960px;margin:0 auto;padding:0 18px;height:50px;display:flex;align-items:center;gap:18px}
.brand{font-size:13.5px;font-weight:600;letter-spacing:.4px;color:var(--fg);text-decoration:none;white-space:nowrap}
.brand i{font-style:normal;color:var(--muted);font-weight:400;font-size:12px;margin-left:7px}
.tabs{display:flex;gap:2px;margin-left:auto}
.tabs a{font-size:13px;color:var(--muted);text-decoration:none;padding:7px 13px;border-radius:7px;line-height:1.3}
.tabs a:hover{color:var(--fg);background:#222a34}
.tabs a.on{color:#fff;background:#2b3542;box-shadow:inset 0 -2px 0 #5b9bd5}
.xlink{margin:24px 0 0}
.xlink a{display:block;background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:14px 16px;text-decoration:none;color:var(--fg)}
.xlink a:hover{border-color:#3c4859;background:#1e242e}
.xlink b{font-size:13.5px;font-weight:600}
.xlink span{display:block;color:var(--muted);font-size:12px;margin-top:5px;line-height:1.6}
@media(max-width:520px){.brand i{display:none}.tabs a{padding:7px 10px;font-size:12.5px}}
</style></head><body>
<nav class="nav"><div class="navin">
<a class="brand" href="./">A 股每日观察<i>sec-dashboard</i></a>
<div class="tabs"><a class="on" href="./">复盘</a><a href="brief.html">简报 · 事件日历</a></div>
</div></nav>
<div class="wrap">
<h1>%(date)s A 股复盘</h1>
<div class="meta">数据时点 %(as_of)s　·　采集于 %(collected)s　·　口径 %(mode)s</div>

<div class="hero">
  <div style="color:var(--muted);font-size:12.5px">情绪温度</div>
  <div class="big" style="color:%(tcolor)s">%(temp).1f <span style="font-size:19px;font-weight:500">%(level)s</span></div>
  <div class="row">
    <div>涨 / 跌 / 平<b>%(up)d / %(down)d / %(flat)d</b></div>
    <div>涨停 / 跌停<b>%(lu)d / %(ld)d</b></div>
    <div>封板率<b>%(seal).2f%%</b></div>
    <div>最高板<b>%(hb)d 板</b></div>
    <div>成交额<b>%(amt)s</b></div>
    <div>建议仓位<b>%(pos)s</b></div>
  </div>
</div>

<div class="card"><h2>当日结论 · 规则自动生成</h2>%(narr)s
<div class="note">以上由规则模板生成，不是模型撰写的判断。它只能归纳「今天发生了什么」，不能预判明天，
也读不到政策、研报、公告这类盘外信息——那些仍然需要人看。</div></div>

<div class="card"><h2>情绪分项</h2><table>
<thead><tr><th>分项</th><th>数值</th><th>权重%%</th><th>得分</th></tr></thead>
<tbody>%(items)s</tbody></table>
<div class="note">样本不足 20 个交易日时用经验阈值映射；达到 20 天后自动切换为历史分位。<br>
首板晋级率缺失表示没有前一交易日的涨停数，该分项不参与加权。</div></div>

<div class="card"><h2>历史轨迹（近 30 个交易日）</h2><table>
<thead><tr><th>日期</th><th>温度</th><th>状态</th><th>封板率</th><th>晋级率</th><th>涨停</th></tr></thead>
<tbody>%(hist)s</tbody></table></div>

<div class="card"><h2>热门板块</h2><table>
<thead><tr><th>板块</th><th>涨停</th><th>最高板</th><th>成分</th></tr></thead>
<tbody>%(sectors)s</tbody></table></div>

<div class="card"><h2>涨停驱动词 TOP20 · 同花顺异动解读</h2><table>
<thead><tr><th>驱动词</th><th>涨停</th><th>最高板</th><th style="text-align:left">标的</th></tr></thead>
<tbody>%(drivers)s</tbody></table>
<div class="note">上表与「热门板块」是两个口径：板块表回答<b>哪些行业涨停多</b>，本表回答<b>个股因什么理由涨停</b>。<br>
标记「泛」＝业绩/国资/回购类高频无区分度词，计分已降权；标记「暗线」＝55188 板块榜里根本没有这个词，
站内排名刷不出来，靠驱动词才看得见。<br>
覆盖率 %(drate).0f%%，数据来自同花顺异动解读。该接口每只股票只保留<b>最近一次异动</b>，
非当日记录一律丢弃，因此<b>不能回看历史</b>，只能逐日采集沉淀。</div></div>

<div class="card"><h2>下一交易日题材前瞻（%(theme_date)s）</h2><table>
<thead><tr><th>题材</th><th>标题</th><th>累计</th><th>涨停</th></tr></thead>
<tbody>%(themes)s</tbody></table>
<div class="note">该表 trade_date 指向下一交易日，属"明日预案"而非当日表现，切勿用于回测当日涨幅。</div></div>

<div class="xlink"><a href="brief.html"><b>下一栏目：简报 · 事件日历 →</b>
<span>未来数个交易日的催化事件、跨市场基准（美/港/油/汇率）、券商口径。
数字由正则抽取、多篇投票合并；因果判断一律不采信，只当排期与校验用。</span></a></div>

<div class="note">本页为盘后快照，非实时行情。数据来自 55188 sec.php 公开接口，仅为个人复盘记录，不构成投资建议。</div>
</div></body></html>""" % {
        "date": snap["trade_date"], "as_of": snap["as_of"], "collected": snap["collected_at"],
        "mode": "历史分位" if t["mode"] == "rank" else "经验阈值",
        "tcolor": "#f2555a" if t["score"] >= 60 else ("#e8a04a" if t["score"] >= 45 else "#5b9bd5"),
        "temp": t["score"], "level": t["level"],
        "up": b["up"], "down": b["down"], "flat": b["flat"],
        "lu": b["limit_up"], "ld": b["limit_down"], "seal": dv["seal_rate"],
        "hb": snap["sentiment"]["max_board"], "amt": yi(b["amount"]),
        "pos": rg["pos"] + "（" + rg["label"] + "）",
        "narr": narr_rows,
        "items": rows, "hist": hist_rows or '<tr><td colspan="6">仅 1 个交易日</td></tr>',
        "sectors": sec_rows or '<tr><td colspan="4">无数据</td></tr>',
        "drivers": drv_rows or '<tr><td colspan="4">未采集（--no-reasons 或抓取失败）</td></tr>',
        "drate": num((dcov or {}).get("rate")),
        "theme_date": snap["themes"]["date"],
        "themes": th_rows or '<tr><td colspan="4">无数据</td></tr>',
    }


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-html", action="store_true", help="只存数据不生成页面")
    ap.add_argument("--show", action="store_true", help="只打印不落盘")
    ap.add_argument("--offline", metavar="RAW_DIR", help="从本地 JSON 目录复算，不联网")
    ap.add_argument("--no-reasons", action="store_true",
                    help="跳过同花顺涨停驱动词抓取（网络受限时用）")
    args = ap.parse_args()

    snap = collect(args.offline)
    series = load_series()
    others = [x for x in series if x["date"] != snap["trade_date"]]
    others.sort(key=lambda x: x["date"])
    prev = others[-1] if others else None

    # 驱动词：55188 只有板块口径，这里补「个股为什么涨停」那一层
    if reasons and not args.no_reasons:
        try:
            rd = reasons.attach(snap, os.path.join(DATA, "reasons"))
            if rd:
                snap["drivers"] = rd["drivers"]
                snap["driver_coverage"] = rd["coverage"]
                snap["reason_records"] = rd["records"]
        except Exception as e:
            print("  ⚠ 驱动词抓取失败（主流程继续）: %s" % e)
            snap.setdefault("degraded", []).append("reasons")
    elif not reasons:
        snap.setdefault("degraded", []).append("reasons(no module)")

    dv = derive(snap, others, prev)
    brief(snap, dv)

    if args.show:
        return

    series = [x for x in series if x["date"] != snap["trade_date"]]
    series.append(to_row(snap, dv))
    series.sort(key=lambda x: x["date"])
    day = save(snap, dv, series)
    print("\n已写入 data/snapshots/%s.json  data/series.json（%d 个交易日）" % (day, len(series)))

    if not args.no_html:
        html = render(snap, dv, series)
        with open(os.path.join(DATA, "report.html"), "w", encoding="utf-8") as f:
            f.write(html)
        print("已生成 data/report.html（%.1f KB）" % (len(html.encode()) / 1024))


if __name__ == "__main__":
    main()
