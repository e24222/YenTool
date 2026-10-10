#!/usr/bin/env python3
"""台股每日掃描：抓取上市/上櫃收盤行情與三大法人，計算技術指標，輸出 stock/data/latest.json。

用法：
  python scripts/stock_scan.py              # 補齊缺少的交易日 + 重新分析
  python scripts/stock_scan.py --analyze    # 只用現有資料重新分析（不連網）
資料來源：臺灣證券交易所 TWSE、證券櫃檯買賣中心 TPEx 公開資料。
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import ssl
import sys
import time
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "stock", "data")
DAILY = os.path.join(ROOT, "daily")
LOOKBACK_DAYS = 140          # 往回補多少個日曆天（約 95 個交易日，足夠算 MA60）
KEEP_DAYS = 130              # 分析時最多使用幾個交易日
DELAY = 3.5                  # 證交所限流，每次請求之間的秒數
TW = dt.timezone(dt.timedelta(hours=8))

# Python 3.13+ 預設啟用 X509 strict，證交所憑證缺少 Subject Key Identifier 會驗證失敗
CTX = ssl.create_default_context()
CTX.verify_flags &= ~ssl.VERIFY_X509_STRICT
_last = [0.0]


def get_json(url, retries=4):
    for i in range(retries):
        wait = DELAY - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (YenTool stock scan)"})
            with urllib.request.urlopen(req, timeout=40, context=CTX) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"  retry {i + 1}/{retries} {url[:90]}… {e}", file=sys.stderr)
            time.sleep(5 * (i + 1))
    return None


def num(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return s
    s = str(s).replace(",", "").replace("%", "").strip()
    if s in ("", "-", "--", "---", "X", "N/A", "除息", "除權"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def col(fields, *names):
    """依欄位名稱（子字串）找索引。"""
    for n in names:
        for i, f in enumerate(fields):
            if n in f:
                return i
    return None


def is_stock(code):
    return bool(re.fullmatch(r"[1-9]\d{3}", code))


# ───────────────────────── 抓取 ─────────────────────────

def fetch_day(d):
    """回傳該日資料 dict；非交易日回傳 None。"""
    ymd = d.strftime("%Y%m%d")
    mi = get_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ymd}&type=ALLBUT0999&response=json")
    if not mi or mi.get("stat") != "OK":
        return None
    stocks, index, breadth = {}, None, {}
    for t in mi.get("tables", []):
        f = t.get("fields") or []
        title = t.get("title", "")
        if "證券代號" in f and "收盤價" in f:
            ic, nm = f.index("證券代號"), f.index("證券名稱")
            iv, ia = f.index("成交股數"), f.index("成交金額")
            io, ih, il, icl = f.index("開盤價"), f.index("最高價"), f.index("最低價"), f.index("收盤價")
            for r in t["data"]:
                if not is_stock(r[ic]):
                    continue
                c = num(r[icl])
                if c is None:
                    continue
                stocks[r[ic]] = [r[nm].strip(), "TWSE", num(r[io]) or c, num(r[ih]) or c, num(r[il]) or c, c,
                                 int(num(r[iv]) or 0), int(num(r[ia]) or 0), 0, 0, 0]
        elif "價格指數" in title and "臺灣證券交易所" in title:
            for r in t["data"]:
                if r[0].strip() == "發行量加權股價指數":
                    sign = -1 if "-" in re.sub(r"<[^>]+>", "", r[2]) else 1
                    index = {"close": num(r[1]), "chg": sign * num(r[3]), "pct": sign * num(r[4])}
        elif "漲跌證券數" in title:
            for r in t["data"]:
                m = re.match(r"([\d,]+)\((\d+)\)", r[2].replace(",", "")) if len(r) > 2 else None
                if r[0].startswith("上漲") and m:
                    breadth["up"], breadth["limit_up"] = int(m.group(1)), int(m.group(2))
                elif r[0].startswith("下跌") and m:
                    breadth["down"], breadth["limit_down"] = int(m.group(1)), int(m.group(2))
                elif r[0].startswith("持平"):
                    breadth["flat"] = int(num(r[2].split("(")[0]) or 0)
    if not stocks:
        return None

    # 上市三大法人
    t86 = get_json(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={ymd}&selectType=ALLBUT0999&response=json")
    if t86 and t86.get("stat") == "OK":
        f = t86["fields"]
        ic = col(f, "證券代號")
        ifr = col(f, "外陸資買賣超股數(不含外資自營商)", "外資買賣超")
        ifd = col(f, "外資自營商買賣超股數")
        iit = col(f, "投信買賣超股數")
        idl = f.index("自營商買賣超股數")
        for r in t86["data"]:
            s = stocks.get(r[ic].strip())
            if s:
                s[8] = int((num(r[ifr]) or 0) + (num(r[ifd]) or 0)) if ifr is not None else 0
                s[9] = int(num(r[iit]) or 0) if iit is not None else 0
                s[10] = int(num(r[idl]) or 0) if idl is not None else 0

    # 上櫃行情（回傳日期若不等於請求日，代表該日無資料）
    roc = f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"
    slash = d.strftime("%Y/%m/%d")
    q = get_json(f"https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={slash}&type=EW&response=json")
    if q and q.get("tables") and q["tables"][0].get("date") == roc and q["tables"][0].get("data"):
        t = q["tables"][0]
        f = t["fields"]
        ic, nm = col(f, "代號"), col(f, "名稱")
        icl, io, ih, il = col(f, "收盤"), col(f, "開盤"), col(f, "最高"), col(f, "最低")
        iv, ia = col(f, "成交股數"), col(f, "成交金額")
        for r in t["data"]:
            code = r[ic].strip()
            if not is_stock(code):
                continue
            c = num(r[icl])
            if c is None:
                continue
            stocks[code] = [r[nm].strip(), "TPEx", num(r[io]) or c, num(r[ih]) or c, num(r[il]) or c, c,
                            int(num(r[iv]) or 0), int(num(r[ia]) or 0), 0, 0, 0]
        ins = get_json(f"https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={slash}&response=json")
        if ins and ins.get("tables") and ins["tables"][0].get("date") == roc and ins["tables"][0].get("data"):
            for r in ins["tables"][0]["data"]:
                s = stocks.get(r[0].strip())
                if s and s[1] == "TPEx" and len(r) > 22:
                    s[8], s[9], s[10] = int(num(r[10]) or 0), int(num(r[13]) or 0), int(num(r[22]) or 0)

    return {"date": d.isoformat(), "index": index, "breadth": breadth, "stocks": stocks}


def fetch_fundamentals(d):
    """本益比/殖利率/淨值比 + 月營收，只抓最新交易日。"""
    fund = {}
    ymd = d.strftime("%Y%m%d")
    r = get_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d?date={ymd}&selectType=ALL&response=json")
    if r and r.get("stat") == "OK":
        f = r["fields"]
        ic, iy, ip, ib = col(f, "證券代號"), col(f, "殖利率"), col(f, "本益比"), col(f, "股價淨值比")
        for row in r["data"]:
            fund[row[ic].strip()] = {"pe": num(row[ip]), "yield": num(row[iy]), "pb": num(row[ib])}
    r = get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis")
    for row in r or []:
        fund[row["SecuritiesCompanyCode"].strip()] = {"pe": num(row.get("PriceEarningRatio")),
                                                      "yield": num(row.get("YieldRatio")),
                                                      "pb": num(row.get("PriceBookRatio"))}
    for url in ("https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
                "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"):
        for row in get_json(url) or []:
            code = (row.get("公司代號") or "").strip()
            yoy = mom = None
            for k, v in row.items():
                if "去年同月增減" in k:
                    yoy = num(v)
                elif "上月比較增減" in k:
                    mom = num(v)
            if code:
                fund.setdefault(code, {}).update({"rev_yoy": yoy, "rev_mom": mom})
    return fund


# ───────────────────────── 指標 ─────────────────────────

def sma(a, n):
    return sum(a[-n:]) / n if len(a) >= n else None


def ema_series(a, n):
    k, out, e = 2 / (n + 1), [], None
    for x in a:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    if len(closes) <= n:
        return None
    g = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    l = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    for i in range(n, len(g)):
        ag, al = (ag * (n - 1) + g[i]) / n, (al * (n - 1) + l[i]) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def kd(h, l, c, n=9):
    """台灣慣用 KD：K=2/3*前K+1/3*RSV，D 同理，初值 50。回傳最近兩日 (K, D)。"""
    if len(c) < n + 1:
        return None
    k = d = 50.0
    ks, ds = [], []
    for i in range(n - 1, len(c)):
        hh, ll = max(h[i - n + 1:i + 1]), min(l[i - n + 1:i + 1])
        rsv = 50.0 if hh == ll else (c[i] - ll) / (hh - ll) * 100
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        ks.append(k)
        ds.append(d)
    return ks[-1], ds[-1], ks[-2], ds[-2]


def macd(c):
    if len(c) < 35:
        return None
    dif = [a - b for a, b in zip(ema_series(c, 12), ema_series(c, 26))]
    sig = ema_series(dif, 9)
    hist = [a - b for a, b in zip(dif, sig)]
    return dif[-1], sig[-1], hist[-1], hist[-2]


def streak(vals):
    n = 0
    for v in reversed(vals):
        if v > 0:
            n += 1
        else:
            break
    return n


def r2(x, p=2):
    return None if x is None else round(x, p)


def analyze_stock(code, rows, fund):
    """rows: [(date, name, mkt, o, h, l, c, vol, amt, fi, it, dl), ...] 依日期遞增"""
    c = [r[6] for r in rows]
    h = [r[4] for r in rows]
    lo = [r[5] for r in rows]
    v = [r[7] / 1000 for r in rows]      # 張
    last = rows[-1]
    if len(c) < 20:
        return None
    price = c[-1]
    prev = c[-2]
    ma5, ma10, ma20, ma60 = sma(c, 5), sma(c, 10), sma(c, 20), sma(c, 60)
    ma20_prev = sum(c[-25:-5]) / 20 if len(c) >= 25 else None
    v5, v20 = sma(v, 5), sma(v, 20)
    vr = v[-1] / (sum(v[-6:-1]) / 5) if len(v) >= 6 and sum(v[-6:-1]) > 0 else None
    rs = rsi(c)
    kdv = kd(h, lo, c)
    mc = macd(c)
    sd = None
    bbpos = bbw = bbup = None
    if len(c) >= 20:
        m = ma20
        sd = (sum((x - m) ** 2 for x in c[-20:]) / 20) ** 0.5
        up, dn = m + 2 * sd, m - 2 * sd
        bbup = up
        bbpos = (price - dn) / (up - dn) * 100 if up != dn else 50
        bbw = (up - dn) / m * 100
    def ret(n):
        return (price / c[-n - 1] - 1) * 100 if len(c) > n else None
    hi20_prev = max(h[-21:-1]) if len(h) >= 21 else None
    hi60 = max(c[-60:]) if len(c) >= 30 else None
    fi = [r[9] / 1000 for r in rows]
    it = [r[10] / 1000 for r in rows]
    dl = [r[11] / 1000 for r in rows]
    f = fund.get(code, {})

    sig = []
    if ma5 and ma20 and ma60 and ma5 > ma20 > ma60 and price > ma5:
        sig.append("多頭排列")
    if ma20 and price > ma20:
        sig.append("站上月線")
    if ma60 and price > ma60:
        sig.append("站上季線")
    if ma20 and ma20_prev and ma20 > ma20_prev:
        sig.append("月線上揚")
    if kdv and kdv[0] > kdv[1] and kdv[2] <= kdv[3]:
        sig.append("KD黃金交叉")
    if mc and mc[2] > 0 >= mc[3]:
        sig.append("MACD翻紅")
    if hi20_prev and price > hi20_prev:
        sig.append("突破20日高")
    if hi60 and price >= hi60 * 0.98:
        sig.append("接近60日高")
    if vr and vr >= 2:
        sig.append("爆量")
    if bbup and price > bbup:
        sig.append("突破布林上軌")
    if rs is not None and rs < 30:
        sig.append("RSI超賣")
    if rs is not None and rs >= 70:
        sig.append("RSI強勢")
    fs, its = streak(fi), streak(it)
    if fs >= 3:
        sig.append("外資連買")
    if its >= 3:
        sig.append("投信連買")
    if f.get("rev_yoy") is not None and f["rev_yoy"] >= 20:
        sig.append("營收年增20%")

    s = 0
    s += 5 * (ma5 is not None and price > ma5) + 8 * ("站上月線" in sig) + 7 * ("站上季線" in sig) + 10 * ("多頭排列" in sig)
    if mc:
        s += 6 * (mc[2] > 0) + 4 * (mc[2] > mc[3])
    if kdv:
        s += 5 * (kdv[0] > kdv[1])
    if rs is not None:
        s += 5 * (50 <= rs <= 75) - 5 * (rs > 85)
    r20 = ret(20)
    s += 5 * (r20 is not None and r20 > 0)
    if vr:
        s += 4 * (vr >= 1.2) + 4 * (vr >= 1.5)
    s += 7 * ("突破20日高" in sig)
    s += 5 * (fi[-1] > 0) + 5 * (it[-1] > 0) + 5 * (fs >= 3) + 5 * (its >= 3)
    if f.get("rev_yoy") is not None:
        s += 5 * (f["rev_yoy"] > 10)
    if f.get("pe") is not None:
        s += 5 * (0 < f["pe"] <= 20)

    return {
        "code": code, "name": last[1], "mkt": last[2],
        "c": price, "pct": r2((price / prev - 1) * 100), "vol": int(v[-1]), "amt": r2(last[8] / 1e8),
        "v20": int(v20) if v20 else None, "vr": r2(vr),
        "ma5": r2(ma5), "ma10": r2(ma10), "ma20": r2(ma20), "ma60": r2(ma60),
        "rsi": r2(rs, 1), "k": r2(kdv[0], 1) if kdv else None, "d": r2(kdv[1], 1) if kdv else None,
        "macd": r2(mc[0], 3) if mc else None, "hist": r2(mc[2], 3) if mc else None,
        "bb": r2(bbpos, 1), "bbw": r2(bbw, 1),
        "r5": r2(ret(5)), "r20": r2(r20), "r60": r2(ret(60)),
        "fi": int(fi[-1]), "it": int(it[-1]), "dl": int(dl[-1]),
        "fi5": int(sum(fi[-5:])), "it5": int(sum(it[-5:])),
        "fs": fs, "its": its,
        "pe": f.get("pe"), "yld": f.get("yield"), "pb": f.get("pb"),
        "ry": r2(f.get("rev_yoy"), 1), "rm": r2(f.get("rev_mom"), 1),
        "sig": sig, "score": min(100, s),
    }


def analyze():
    files = sorted(glob.glob(os.path.join(DAILY, "*.json")))[-KEEP_DAYS:]
    if not files:
        sys.exit("沒有任何 daily 資料")
    days = [json.load(open(p, encoding="utf-8")) for p in files]
    try:
        fund = json.load(open(os.path.join(ROOT, "fund.json"), encoding="utf-8"))
    except FileNotFoundError:
        fund = {}
    series = {}
    for d in days:
        for code, s in d["stocks"].items():
            series.setdefault(code, []).append((d["date"], *s))
    latest = days[-1]
    out = []
    for code, rows in series.items():
        if rows[-1][0] != latest["date"]:
            continue                     # 最新交易日沒成交的（停牌/下市）略過
        a = analyze_stock(code, rows, fund)
        if a:
            out.append(a)
    out.sort(key=lambda x: -x["score"])

    # 大盤：近 60 日指數 + 法人買賣超金額（約略，以收盤價估算）
    idx_hist = [{"d": d["date"], "c": d["index"]["close"]} for d in days[-60:] if d.get("index")]
    inst = {"fi": 0, "it": 0, "dl": 0}
    for s in latest["stocks"].values():
        for k, i in (("fi", 8), ("it", 9), ("dl", 10)):
            inst[k] += s[i] * s[5]
    inst = {k: round(v / 1e8, 1) for k, v in inst.items()}
    turnover = round(sum(s[7] for s in latest["stocks"].values()) / 1e8, 0)

    payload = {
        "date": latest["date"], "generated": dt.datetime.now(TW).isoformat(timespec="minutes"),
        "days": len(days), "index": latest.get("index"), "breadth": latest.get("breadth"),
        "index_hist": idx_hist, "inst": inst, "turnover": turnover, "count": len(out), "stocks": out,
    }
    with open(os.path.join(ROOT, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"分析完成：{latest['date']}，{len(out)} 檔，使用 {len(days)} 個交易日")


# ───────────────────────── 主流程 ─────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--analyze", action="store_true", help="只重新分析，不連網")
    args = ap.parse_args()
    os.makedirs(DAILY, exist_ok=True)
    if not args.analyze:
        hp = os.path.join(ROOT, "holidays.json")
        holidays = set(json.load(open(hp)) if os.path.exists(hp) else [])
        today = dt.datetime.now(TW).date()
        have = {os.path.basename(p)[:8] for p in glob.glob(os.path.join(DAILY, "*.json"))}
        todo = []
        for i in range(LOOKBACK_DAYS, -1, -1):
            d = today - dt.timedelta(days=i)
            if d.weekday() >= 5 or d.isoformat() in holidays or d.strftime("%Y%m%d") in have:
                continue
            todo.append(d)
        print(f"待抓取 {len(todo)} 天")
        for d in todo:
            data = fetch_day(d)
            if data:
                with open(os.path.join(DAILY, d.strftime("%Y%m%d") + ".json"), "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
                print(f"  {d} ok ({len(data['stocks'])} 檔)")
            elif d < today:
                holidays.add(d.isoformat())
                print(f"  {d} 休市")
            else:
                print(f"  {d} 今日資料尚未公布")
        with open(hp, "w") as f:
            json.dump(sorted(holidays), f)
        files = sorted(glob.glob(os.path.join(DAILY, "*.json")))
        if files:
            ld = dt.datetime.strptime(os.path.basename(files[-1])[:8], "%Y%m%d").date()
            fp = os.path.join(ROOT, "fund.json")
            stamp = os.path.join(ROOT, ".fund_date")
            if not (os.path.exists(fp) and os.path.exists(stamp) and open(stamp).read() == ld.isoformat()):
                fund = fetch_fundamentals(ld)
                if fund:
                    with open(fp, "w", encoding="utf-8") as f:
                        json.dump(fund, f, ensure_ascii=False, separators=(",", ":"))
                    open(stamp, "w").write(ld.isoformat())
    analyze()


if __name__ == "__main__":
    main()
