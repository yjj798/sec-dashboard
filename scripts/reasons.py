# -*- coding: utf-8 -*-
"""
涨停驱动词抓取与主线聚类（同花顺异动解读，免 Cookie）

数据源（2026-11-01 实测）：
    https://dq.10jqka.com.cn/fuyao/transaction_history/service/v1/get/{market}_{code}.txt
    -> data.anomalyAnalysisList[0]
       {date, tagName(涨停/大涨/跌停/大跌), tagCode, keywordList[], reason}

接口语义（重要）：
    每只股票**只保留最近一次异动**。当日涨停的股票基本能命中当日记录（实测 47/49），
    但**不能回看历史**——某只股票三个月前异动过、今天没异动，拿到的就是三个月前那条。
    因此本模块的日期必须校验，只采信 date == 当日 的记录。
    历史序列无法事后补抓，只能每日采集逐日沉淀。

为什么它比 55188 的板块标签强：
    55188 的 plates 是「行业/板块」维度（医药、航天、大消费），同一只票常只标泛板块；
    同花顺 keywordList 是「个股驱动」维度（脑机接口、青啤拟入主、“羊”字辈），能解释为什么涨。
    实测 10-30：47 只涨停股里 29 只 55188 只给了泛板块，同花顺全部给出具体驱动词。
"""
import json
import os
import time
from collections import Counter, defaultdict

import requests

ABN_URL = "https://dq.10jqka.com.cn/fuyao/transaction_history/service/v1/get/{m}_{c}.txt"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                   "AppleWebKit/605.1.15"),
    "Referer": "https://ms.10jqka.com.cn/",
    "Accept": "application/json, text/plain, */*",
}

# 泛词：高频但无区分度。聚类打分时降权，且不参与「暗线」判定。
GENERIC = {
    "业绩增长", "季报增长", "单季营收增长", "三季报增长", "三季报大增", "三季报高增",
    "三季报扭亏", "三季报减亏", "三季报净利增长", "三季度净利增", "中报增长", "年报增长",
    "业绩预增", "扭亏为盈", "减亏", "扭亏", "回购", "回购注销", "增持", "股权激励",
    "国企改革", "国资控股", "央企", "地方国资", "天津国资", "温州国资", "高股息",
    "高股息精选", "证金持股", "中期分红", "超跌反弹", "低价股",
}
GENERIC_WEIGHT = 0.35
CACHE_V = 2   # 聚类口径变更时 +1，旧缓存只重算不重抓


def _in_view(kw, sec_names):
    """驱动词是否已经在 55188 的板块口径里出现过（含互相包含，如 PCB ⊂ PCB板）。

    判定从严：宁可少标「暗线」，也不要给出一条其实板块榜上看得到的线。
    """
    for n in sec_names:
        if not n:
            continue
        if kw == n or kw in n or n in kw:
            return True
    return False


def market_of(code):
    """6 开头沪市=17，0/3 开头深市=33，4/8 开头北交所=151"""
    c = str(code)
    if c.startswith("6"):
        return "17"
    if c.startswith(("4", "8")):
        return "151"
    return "33"


def fetch_one(code, retry=1):
    url = ABN_URL.format(m=market_of(code), c=code)
    for i in range(retry + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                return None
            j = r.json()
            for x in (j.get("data") or {}).get("anomalyAnalysisList") or []:
                if x.get("date"):
                    return {
                        "date": x["date"],
                        "tag": x.get("tagName") or "",
                        "tag_code": x.get("tagCode") or "",
                        "keywords": [k for k in (x.get("keywordList") or []) if k],
                        "reason": (x.get("reason") or "").strip(),
                    }
            return None
        except Exception:
            if i == retry:
                return None
            time.sleep(0.6)
    return None


def collect_reasons(codes, sleep=0.25, verbose=True):
    """按代码列表抓驱动词，返回 {code: {...}}（不过滤日期）"""
    out, miss = {}, []
    n = len(codes)
    for i, code in enumerate(codes, 1):
        r = fetch_one(code)
        if r:
            out[code] = r
        else:
            miss.append(code)
        if verbose and (i % 10 == 0 or i == n):
            print("    驱动词 %d/%d" % (i, n))
        time.sleep(sleep)
    return out, miss


def cluster(records, trade_date=None):
    """驱动词聚类。records: [{code,name,board,keywords}]

    返回按 score 降序的列表，每项含：
      keyword / count / max_board / score / generic / stocks / in_sector_view
    in_sector_view=False 表示 55188 的板块口径里根本没有这个词 → 暗线
    """
    cnt, holders, maxb = Counter(), defaultdict(list), {}
    for r in records:
        for k in r.get("keywords") or []:
            cnt[k] += 1
            holders[k].append({"code": r["code"], "name": r["name"],
                               "board": r.get("board", 1)})
            maxb[k] = max(maxb.get(k, 0), r.get("board", 1))

    rows = []
    for k, n in cnt.items():
        g = k in GENERIC
        rows.append({
            "keyword": k,
            "count": n,
            "max_board": maxb[k],
            "generic": g,
            "score": round(n * (GENERIC_WEIGHT if g else 1.0)
                           + maxb[k] * 0.5, 2),
            "stocks": sorted(holders[k], key=lambda x: -x["board"]),
        })
    rows.sort(key=lambda x: (-x["score"], -x["count"]))
    return rows


def attach(snap, cache_dir, sleep=0.25, verbose=True):
    """把驱动词挂到 snap 上，返回 {trade_date, records, drivers, coverage}

    落盘 data/reasons/<date>.json。同日重跑命中缓存直接复用（不再打对方接口）；
    聚类口径升级（CACHE_V 变化）时只用缓存里的 records 重算，同样不重抓。
    """
    day = snap.get("trade_date")
    if not day:
        return None
    os.makedirs(cache_dir, exist_ok=True)
    cpath = os.path.join(cache_dir, day + ".json")

    records, coverage, fetched_at = None, None, None
    if os.path.exists(cpath):
        try:
            cached = json.load(open(cpath, encoding="utf-8"))
            if cached.get("trade_date") == day and cached.get("records"):
                records = cached["records"]
                coverage = cached.get("coverage")
                fetched_at = cached.get("fetched_at")
                if cached.get("_v") == CACHE_V:
                    if verbose:
                        print("  驱动词: 命中缓存 %s（%d 只）" % (day, len(records)))
                    return cached
                if verbose:
                    print("  驱动词: 缓存口径 v%s→v%s，用已有 %d 条重算（不重抓）"
                          % (cached.get("_v"), CACHE_V, len(records)))
        except Exception:
            records = None

    if records is None:
        pool = []
        for g in snap.get("ladder") or []:
            for s in g.get("stocks") or []:
                pool.append({"code": s["code"], "name": s["name"],
                             "board": int(g.get("board") or 1),
                             "sec_plates": [p.get("name") for p in s.get("plates") or []]})
        if not pool:
            return None
        if verbose:
            print("  驱动词: 抓取 %d 只涨停股…" % len(pool))

        got, miss = collect_reasons([s["code"] for s in pool], sleep=sleep, verbose=verbose)
        records, stale = [], 0
        for s in pool:
            r = got.get(s["code"])
            if not r:
                continue
            if r["date"] != day:
                stale += 1
                continue  # 非当日记录：是上一次异动，混进来会污染聚类
            records.append({
                "code": s["code"], "name": s["name"], "board": s["board"],
                "sec_plates": s["sec_plates"],
                "tag": r["tag"], "tag_code": r["tag_code"],
                "keywords": r["keywords"],
                "reason": r["reason"],
            })
        coverage = {
            "pool": len(pool),
            "hit_same_day": len(records),
            "hit_any": len(got),
            "stale_skipped": stale,
            "no_record": len(miss),
            "rate": round(len(records) / len(pool) * 100, 1) if pool else 0.0,
        }
        fetched_at = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        if verbose:
            print("  驱动词: 当日命中 %d/%d（%.0f%%），过期跳过 %d，无记录 %d"
                  % (coverage["hit_same_day"], coverage["pool"], coverage["rate"],
                     coverage["stale_skipped"], coverage["no_record"]))

    drivers = cluster(records)

    # 暗线：55188 的板块口径里根本没有这个词
    sec_names = set()
    for x in snap.get("hot_sectors") or []:
        sec_names.add(x.get("name"))
    for g in snap.get("ladder") or []:
        for s in g.get("stocks") or []:
            for p in s.get("plates") or []:
                if p.get("name"):
                    sec_names.add(p["name"])
    for d in drivers:
        d["in_sector_view"] = _in_view(d["keyword"], sec_names)

    data = {
        "_v": CACHE_V,
        "trade_date": day,
        "fetched_at": fetched_at,
        "coverage": coverage,
        "records": records,
        "drivers": drivers,
    }
    json.dump(data, open(cpath, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return data


def summarize(rdata, top=6):
    """把聚类结果压成「广度线 / 高度线 / 暗线」三句话的结构化结论"""
    if not rdata or not rdata.get("drivers"):
        return None
    ds = rdata["drivers"]
    spec = [d for d in ds if not d["generic"] and d["count"] >= 2]
    broad = max(spec, key=lambda d: (d["count"], d["max_board"])) if spec else None
    high = max(spec, key=lambda d: (d["max_board"], d["count"])) if spec else None
    hidden = [d for d in spec if not d["in_sector_view"] and d["count"] >= 2]
    hidden.sort(key=lambda d: (-d["count"], -d["max_board"]))
    return {
        "coverage": rdata["coverage"],
        "broad": broad,
        "high": high,
        "hidden": hidden[:3],
        "top": ds[:top],
    }
