# -*- coding: utf-8 -*-
"""每週自動同步 World Gym 分店資料 → radar/data.json
只更新 wg 區塊,arc(機廳)資料保持不變。
安全機制:抓到的分店數 < 100 視為異常,直接放棄不覆蓋。
"""
import re, json, sys, datetime, concurrent.futures
import urllib.request

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
BASE = 'https://www.worldgymtaiwan.com'

# 官網座標錯誤的人工修正(以 Google Maps 實際位置為準)
COORD_FIXES = {
    'chiayi-xingye': (23.4695983, 120.4340771),
    'kaohsiung-boai': (22.6636463, 120.3035812),
    'new-taipei-xike': (25.0619144, 121.6470307),
    'pingtung-ziyou': (22.6838115, 120.486942),
    'taipei-nanjing': (25.051218, 121.5642089),
    'taoyuan-fuxing': (24.9903136, 121.311398),
    'taoyuan-zhongli-zhongyuan': (24.9624549, 121.2322687),
}

def get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', errors='ignore')

def wgname(h1, slug):
    m = re.search(r'([\u4e00-\u9fffA-Za-z0-9]+店)', h1)
    return m.group(1) if m else slug

def fetch_club(slug):
    try:
        h = get(f'{BASE}/find-a-club/{slug}')
        addr_m = re.search(r'[\u4e00-\u9fff]{2,3}[市縣][\u4e00-\u9fff0-9]{0,15}[路街][\u4e00-\u9fffA-Za-z0-9段巷弄之、－-]{0,25}號[BbF0-9樓之、-]{0,8}', h)
        if not addr_m:
            addr_m = re.search(r'[\u4e00-\u9fff]{2,3}[市縣][\u4e00-\u9fff]{1,4}[區鎮市鄉][\u4e00-\u9fffA-Za-z0-9段巷弄、－-]{1,25}號[BbF0-9樓之、-]{0,8}', h)
        if not addr_m:
            return None  # 無地址視為無效頁(重複/歇業 slug)
        h1_m = re.search(r'class="h1">([^<]+)</div>', h)
        h1 = h1_m.group(1).strip() if h1_m else slug
        t = 'Express' if re.search(r'express', h1, re.I) else ('Sport' if re.search(r'sport', h1, re.I) else '一般')
        if slug in COORD_FIXES:
            la, lo = COORD_FIXES[slug]
        else:
            c = re.search(r'@(2[0-5]\.\d+),(12[0-2]\.\d+)', h)
            if not c:
                return None
            la, lo = float(c.group(1)), float(c.group(2))
        return {'n': wgname(h1, slug), 't': t, 'a': addr_m.group(0), 'la': round(la, 6), 'lo': round(lo, 6)}
    except Exception as e:
        print(f'  warn {slug}: {e}', file=sys.stderr)
        return None

def main():
    sitemap = get(f'{BASE}/sitemap.xml', timeout=60)
    slugs = sorted(set(re.findall(r'/find-a-club/([A-Za-z0-9-]+)', sitemap)))
    print(f'sitemap slugs: {len(slugs)}')
    if len(slugs) < 100:
        print('異常:slug 數量過少,中止'); sys.exit(1)

    wg = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(fetch_club, slugs):
            if r:
                wg.append(r)
    # 依地址去重(官網偶有重複 slug)
    seen = {}
    for x in wg:
        k = x['a']
        if k not in seen or len(x['n']) > len(seen[k]['n']):
            seen[k] = x
    wg = list(seen.values())
    print(f'valid clubs: {len(wg)}')
    if len(wg) < 100:
        print('異常:有效分店過少,中止(不覆蓋舊資料)'); sys.exit(1)

    path = 'radar/data.json'
    data = json.load(open(path, encoding='utf-8'))
    old = json.dumps(data['wg'], ensure_ascii=False, sort_keys=True)
    new = json.dumps(sorted(wg, key=lambda x: x['a']), ensure_ascii=False, sort_keys=True)
    if old == new:
        print('資料無變化'); return
    data['wg'] = sorted(wg, key=lambda x: x['a'])
    today = datetime.date.today().isoformat()
    data['wg_updated'] = today
    data['updated'] = today
    json.dump(data, open(path, 'w', encoding='utf-8'), ensure_ascii=False)
    print(f'已更新:{len(wg)} 間分店')

if __name__ == '__main__':
    main()
