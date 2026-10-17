# coding=utf-8
"""手机好价监控：指定机型优惠 → PushPlus（微信ClawBot，不发服务号）

监测范围（型号白名单）：
- OPPO Find X9 系列：Find X9 / X9 Pro / X9s Pro / X9 Ultra
- OPPO Find X10 系列：Find X10 / X10 Pro Max / X10 E / (未来 Ultra)
- vivo X300 系列：X300 / X300 Pro / X300s / X300 Ultra / X300 E
- vivo X500 系列：X500 / X500 Pro / X500 Pro Max
（前缀匹配，未来新增后缀自动覆盖）

推送规则：
- 好价判断：售价须低于官方【发行价】28% 以上才算好价（RRP_TABLE 发行价表），
  达不到直接静默淘汰——官方发行价原价冒充的"好价"一律不推
- 同一「型号 + 价格」的消息最多推送 3 次，两次间隔至少 30 分钟
  （同款反复被爆料时：首见即推，30 分钟后若还在则第 2 次，60 分钟后第 3 次，之后不再推）
- 单条消息最多 10 条，超出自动拆成多条消息依次推送（间隔 5 秒、单轮最多 4 条）；
  单轮发不完的自动留到下一轮巡检续推——全部推到微信，不需要去别处查看
- 降价产生新价格 = 新消息，正常推送
- 资讯（IT之家/快科技）按链接去重 + 跨来源标题归一化去重，只推一次；资讯无价格不做过滤
- 首次运行仅记录基线不推送

信息源（8 个）：
1. 什么值得买 · 手机分类好价 https://www.smzdm.com/fenlei/shouji/
2. 什么值得买 · 移动站好价榜 https://m.smzdm.com/top/shouji/（SSR，与 1 高度互补）
3. 什么值得买 · 官方 API 手机分类流 api.smzdm.com/v1/list?category_id=165/389/4953
4. 什么值得买 · 官方 API 关键词搜索 api.smzdm.com/v1/list?keyword=（跨频道兜底）
5. 中关村在线 · 手机报价列表 detail.zol.com.cn（SSR 参考价，3 页约 140+ 款）
6. 快科技 https://news.mydrivers.com/（机型促销资讯）
7. IT之家 RSS https://www.ithome.com/rss/（机型促销资讯）
8. OPPO 官方商城盯价 https://www.opposhop.cn（7 款目标机型直降第一手）

环境变量：PUSHPLUS_TOKEN（必填）
          PUSHPLUS_CHANNELS（默认 clawbot，即微信ClawBot；好价不推服务号）
用法：python scripts/phone_deal.py [--test | --dry-run]
"""

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))  # 状态/展示统一北京时间：Actions 跑在 UTC、本地在北京，混写会打乱 30 分钟轮转


def now_cn() -> datetime:
    """当前北京时间（naive），state 的 last/updated 与消息展示全部用它"""
    return datetime.now(CN_TZ).replace(tzinfo=None)
from pathlib import Path

import requests

# Windows 控制台默认 GBK，日志含 ¥ 等字符会 UnicodeEncodeError → 强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ──────────────────────────── 配置 ────────────────────────────

MAX_PUSH = 10                 # 单条消息最多条目数（超出自动拆成多条消息）
MAX_MSGS_PER_RUN = 4          # 单轮最多推送消息数（分批发完，余量下轮续推）
MSG_GAP_SEC = 5               # 分批消息间隔秒（PushPlus 微信渠道限 1 分钟 5 次）
MAX_SAME_PUSH = 3             # 同一「型号+价格」最多推送次数
SAME_PUSH_GAP_MIN = 30        # 同款两次推送最小间隔（分钟）
STATE_KEEP = 800              # 状态保留条数
STATE_MAX_AGE_DAYS = 60       # 状态保留天数
STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "phone_deal_state.json"
LEGACY_PATH = Path(__file__).resolve().parent.parent / "data" / "phone_deal_history.json"

PUSH_URL = "https://www.pushplus.plus/send"

# ── 型号白名单（前缀匹配，覆盖全系列含未来后缀） ──
# 范围约定：仅 OPPO Find X9/X10 + vivo X300/X500 各系列（勿扩）
MODEL_RE = re.compile(
    "|".join([
        r"(?:OPPO\s*)?Find\s*X\s?(?:9|10)(?!\d)",   # OPPO Find X9 / X10 系列
        r"OPPO\s*X\s?(?:9|10)(?!\d)",                # 少数爆料省略 Find
        r"vivo\s*X\s?(?:300|500)(?!\d)",             # vivo X300 / X500 系列
    ]),
    re.I,
)

# 配件标题一律不进推送（“Find X9 充电器”这类误配）
ACC_RE = re.compile(
    r"充电器|数据线|充电线|手机壳|保护壳|钢化膜|贴膜|耳机|适配器|背夹|"
    r"延保|礼品卡|以旧换新|清洁套装"
)


def model_hit(title: str):
    """机型匹配（排除配件标题），返回 Match | None"""
    if ACC_RE.search(title):
        return None
    return MODEL_RE.search(title)

# IT之家资讯需同时命中的促销词
DEAL_WORD_RE = re.compile(
    r"优惠|降价|好价|直降|到手|券|促销|秒杀|补贴|首发价|首销|开售|预售|立减|白条|免息|11\.11|618|双11"
)

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

SMZDM_URL = "https://www.smzdm.com/fenlei/shouji/"
ITHOME_RSS = "https://www.ithome.com/rss/"

# 源四：什么值得买移动站好价榜（SSR，数据内嵌 window.__NUXT__，与 PC 分类页高度互补）
SMZDM_TOP_URLS = [
    "https://m.smzdm.com/top/shouji/",
    "https://m.smzdm.com/top/zhinengshouji/",
]
UA_MOBILE = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
# NUXT 序列化行：id → 标题 → 展示价 → 结构化价格（可能是字面量或变量）
TOP_ROW_RE = re.compile(
    r'article_id:"(\d+)"'
    r'.*?article_title:"((?:[^"\\]|\\.)*)"'
    r'.*?article_subtitle:"((?:[^"\\]|\\.)*)"'
    r'.*?article_digital_price:("?[0-9.]+"?|[A-Za-z]\w*)',
    re.S,
)

# 源三/四：smzdm 官方 JSON API（无签名，带 Referer 即可；JSON 结构化 + 可翻页）
# 2026-10 实测：category_id 为精确分类（165 手机通讯 / 389 手机 / 4953 安卓手机，条目集不同需全抓）
# keyword 为站内全文搜索（search.smzdm.com 网页版 202 反爬，此 API 不拦）
SMZDM_API = "https://api.smzdm.com/v1/list"
SMZDM_API_H = {**UA, "Referer": "https://www.smzdm.com/"}
SMZDM_API_CATS = {"手机通讯": 165, "手机": 389, "安卓手机": 4953}
SMZDM_API_KEYWORDS = ["Find X9", "Find X10", "vivo X300", "vivo X500"]

# 源五：中关村在线手机报价列表（SSR：alt=机型(配置) / b.price-type=参考价）
ZOL_URLS = [
    "https://detail.zol.com.cn/cell_phone/",
    "https://detail.zol.com.cn/cell_phone_index/subcate57_list_2.html",
    "https://detail.zol.com.cn/cell_phone_index/subcate57_list_3.html",
]

# 源六：快科技列表页（响应头无 charset，页内 meta 为 utf-8）
MYDRIVERS_URL = "https://news.mydrivers.com/"

# 源三：OPPO 官方商城（opposhop.cn）目标机型商品页 —— 官方直降第一手
# 价格字段 buyPrice（页内内联 JS），价格一变即视为新消息
OPPO_OFFICIAL_SKUS = {
    "36848": "OPPO Find X9",
    "36878": "OPPO Find X9 Pro",
    "39805": "OPPO Find X9s Pro",
    "39829": "OPPO Find X9 Ultra",
    "45215": "OPPO Find X10",
    "45203": "OPPO Find X10 Pro Max",
    "45222": "OPPO Find X10 E",
}

# ── 好价阈值：低于发行价 28% 才算好价 ──
GOOD_DEAL_DISCOUNT = 0.28        # 好价判定：售价 ≤ 发行价 ×(1-28%)

# ── 发行价表（发布会首发官方价，配置 → 价格） ──
RRP_TABLE = {
    # OPPO Find X9 系列（2025-10-16 发布）
    "x9": {"12+256": 4399, "16+256": 4699, "12+512": 4999, "16+512": 5299, "16+1tb": 5799},
    "x9pro": {"12+256": 5299, "12+512": 5699, "16+512": 5999, "16+1tb": 6699},
    "x9spro": {"12+256": 5299, "12+512": 5699, "16+512": 5999, "16+1tb": 6999},
    "x9ultra": {"12+256": 7499, "12+512": 7999, "16+512": 8499,
                "16+1tb": 9299, "16+1tb卫星": 9499},
    # OPPO Find X10 系列（2026-09-22 发布）
    "x10": {"12+256": 5499, "12+512": 5999, "16+512": 6499, "16+1tb": 7499},
    "x10promax": {"12+256": 6799, "12+512": 7499, "16+512": 7999, "16+1tb": 8999},
    "x10e": {"12+256": 4999, "16+512": 5499},
    # vivo X300 系列（2025-10-13 / X300s·Ultra 2026-03-30 / X300E 2026-07-27）
    "x300": {"12+256": 4399, "16+256": 4699, "12+512": 4999, "16+512": 5299, "16+1tb": 5799},
    "x300pro": {"12+256": 5299, "16+512": 5999, "16+1tb": 6699, "16+1tb卫星": 8299},
    "x300s": {"12+256": 4999, "12+512": 5499, "16+512": 5999, "16+1tb": 6999},
    "x300ultra": {"12+256": 6999, "12+512": 7499, "16+512": 7999,
                  "16+1tb": 8999, "16+1tb卫星": 8999},
    "x300e": {"12+256": 4799, "12+512": 5299},
    # vivo X500 系列（2026-09-21 发布）
    "x500": {"12+256": 5499, "12+512": 5999, "16+512": 6499, "12+1tb": 6999},
    "x500pro": {"12+256": 6499, "12+512": 7499, "16+512": 7999, "12+1tb": 8499, "16+1tb": 8999},
    "x500promax": {"12+256": 6999, "12+512": 7999, "16+512": 8499,
                   "12+1tb": 8999, "16+1tb": 9499, "16+1tb卫星": 9499},
}

# 完整型号提取（变体顺序：长后缀优先），返回如 x9 / x9spro / x10promax / x300ultra
# 结构：(正则, 键前缀)，组1 = 型号后缀
_FULL_MODEL_RES = [
    (re.compile(
        r"(?:OPPO\s+)?Find\s*X\s*"
        r"(9s\s*pro|9\s*pro|9\s*ultra|9s(?!\d)|9(?!\d)"
        r"|10\s*pro\s*max|10\s*pro|10\s*ultra|10\s*e|10s(?!\d)|10(?!\d))", re.I), "x"),
    (re.compile(
        r"OPPO\s+X\s*(9s\s*pro|9\s*pro|9\s*ultra|9s(?!\d)|9(?!\d)"
        r"|10\s*pro\s*max|10\s*e|10(?!\d))", re.I), "x"),
    (re.compile(
        r"vivo\s*X\s*(500\s*pro\s*max|500\s*pro|500\s*ultra|500(?!\d)"
        r"|300\s*pro|300\s*ultra|300\s*s|300\s*e|300(?!\d))", re.I), "x"),
]

CONFIG_RE = re.compile(r"(12|16|24)\s*(?:GB)?\s*[+＋\-—×xX]\s*(256|512|128|1\s*[Tt][Bb]?)(?!\d)", re.I)


def log(msg: str) -> None:
    print(f"[{now_cn().strftime('%H:%M:%S')}] {msg}", flush=True)


def full_model_id(text: str) -> str:
    """标题 → 完整型号键：'OPPO Find X9s Pro 乘风青' → 'x9spro'；识别不出返回 ''"""
    for pat, prefix in _FULL_MODEL_RES:
        m = pat.search(text)
        if m:
            return prefix + re.sub(r"[\s_]+", "", m.group(1).lower())
    return ""


def config_key(text: str) -> str:
    """标题 → 配置键：'16GB+512GB' → '16+512'；'16+1TB' → '16+1tb'"""
    m = CONFIG_RE.search(text)
    if not m:
        return ""
    ram = m.group(1)
    rom = m.group(2).lower().replace(" ", "")
    rom = "1tb" if rom in ("1t", "1tb") else rom
    return f"{ram}+{rom}"


def is_good_deal(item: dict) -> tuple[bool, str]:
    """好价判断：售价 ≤ 发行价×(1-28%) 才算好价。返回 (通过?, 说明)"""
    title = item["title"]
    try:
        price = float(item["price"])
    except (TypeError, ValueError):
        return False, "无价格"
    mid = full_model_id(title)
    if not mid:
        return True, ""                      # 识别不出具体机型 → 放行（宁多勿漏）
    table = RRP_TABLE.get(mid)
    if not table:
        return True, ""                      # 新机型暂无发行价 → 放行，待补表
    cfg = config_key(title)
    if cfg and "卫星" in title and f"{cfg}卫星" in table:
        cfg += "卫星"
    rrp = table.get(cfg)
    if rrp is None:
        rrp = min(table.values())            # 配置未知 → 按该机型最低发行价从严判断
    limit = rrp * (1 - GOOD_DEAL_DISCOUNT)
    return price <= limit, f"¥{price:.0f}/发行价¥{rrp}，门槛¥{limit:.0f}"


def model_key(match_text: str) -> str:
    """匹配文本 → 归一化型号键：'OPPO Find X9 Pro' → 'findx9'"""
    k = re.sub(r"[^a-z0-9]", "", match_text.lower())
    return k.replace("oppo", "").replace("vivo", "")


# ──────────────────────── 源一：什么值得买好价 ────────────────────────

def fetch_smzdm() -> list[dict]:
    r = requests.get(SMZDM_URL, headers=UA, timeout=25)
    r.raise_for_status()
    text = r.text
    items: dict[str, dict] = {}
    pat = re.compile(r'href="(https://www\.smzdm\.com/p/(\d+)/)"[^>]*>(?:<!--.*?-->)?([^<]{6,150})</a>', re.S)
    for m in pat.finditer(text):
        url, pid, title = m.group(1), m.group(2), m.group(3)
        if pid in items:
            continue
        title = re.sub(r"\s+", " ", re.sub(r"<.*?>", "", title)).strip()
        mm = model_hit(title)
        if not mm:
            continue
        win = text[m.start(): m.start() + 1500]
        pm = re.search(r'class="[^"]*price[^"]*"[^>]*>\s*[¥￥]?\s*([\d]{2,6}(?:\.\d{1,2})?)', win)
        price = pm.group(1) if pm else ""
        mk = model_key(mm.group(0))
        items[pid] = {
            "url": url, "title": title, "price": price,
            "src": "smzdm", "kind": "deal",
            "key": f"{mk}|{price}" if price else f"{mk}|{url}",
            "model": mk,
        }
    out = list(items.values())
    log(f"[smzdm] 命中机型好价 {len(out)} 条")
    return out


# ──────────────── 源八：OPPO 官方商城直降（第一手） ────────────────

def fetch_oppo_official() -> list[dict]:
    items = []
    for sid, name in OPPO_OFFICIAL_SKUS.items():
        try:
            r = requests.get(f"https://www.opposhop.cn/cn/web/products/{sid}.html",
                             headers=UA, timeout=20)
            r.raise_for_status()
            page = r.text
        except requests.RequestException as e:
            log(f"[oppo官方] {name} 抓取失败: {type(e).__name__}")
            continue
        # 页面价格字段不统一：部分页是 buyPrice，部分页是 price（末位=本机价）
        pm = re.search(r'buyPrice\s*:\s*"([0-9]{4,6}(?:\.\d+)?)"', page)
        if pm:
            price = pm.group(1)
        else:
            found = re.findall(r'price\s*:\s*"([0-9]{4,6}(?:\.\d+)?)"', page)
            price = found[-1] if found else ""
        if not price:
            log(f"[oppo官方] {name} 未取到价格字段")
            continue
        tm = (re.search(r'<h1 class="tit"[^>]*>([^<]+)</h1>', page)
              or re.search(r'<title>([^<]+)</title>', page))
        detail = re.sub(r"\s+", " ", tm.group(1)).strip() if tm else name
        items.append({
            "url": f"https://www.opposhop.cn/cn/m/product/index?skuId={sid}",
            "title": f"OPPO官方 · {detail}",
            "price": price,
            "src": "oppo", "kind": "deal",
            "key": f"off|{sid}|{price}",
            "model": f"off{sid}",
        })
    log(f"[oppo官方] 盯价 {len(items)}/{len(OPPO_OFFICIAL_SKUS)} 款")
    return items


# ──────────────── 源二：smzdm 移动站好价榜 ────────────────

def fetch_smzdm_top() -> list[dict]:
    items: dict[str, dict] = {}
    for url in SMZDM_TOP_URLS:
        try:
            r = requests.get(url, headers=UA_MOBILE, timeout=25)
            r.raise_for_status()
            page = r.text
        except requests.RequestException as e:
            log(f"[smzdm榜] {url} 抓取失败: {type(e).__name__}")
            continue
        for m in TOP_ROW_RE.finditer(page):
            pid, title, subtitle, price_raw = m.groups()
            title = re.sub(r"\s+", " ", title.replace("\\u002F", "/")).strip()
            subtitle = subtitle.replace("\\u002F", "/")
            if price_raw.startswith('"'):
                price = price_raw.strip('"')
            else:   # 变量引用 → 从展示价 “xxx元” 提取
                pm = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*元", subtitle)
                price = pm.group(1) if pm else ""
            if not price:
                continue
            mm = model_hit(title)
            if not mm:
                continue
            if pid in items:
                continue
            mk = model_key(mm.group(0))
            items[pid] = {
                "url": f"https://www.smzdm.com/p/{pid}/",
                "title": title, "price": price,
                "src": "smzdm_top", "kind": "deal",
                "key": f"{mk}|{price}",
                "model": mk,
            }
    out = list(items.values())
    log(f"[smzdm榜] 命中机型好价 {len(out)} 条")
    return out


# ──────────────── 源三/四：smzdm 官方 JSON API ────────────────

def _smzdm_api_rows(params: dict) -> list[dict]:
    """调 smzdm 官方列表 API，返回 rows；失败返回 []"""
    query = {"limit": 20, "offset": 0, "type": "youhui", "order": "time", **params}
    try:
        r = requests.get(SMZDM_API, params=query, headers=SMZDM_API_H, timeout=25)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log(f"[smzdmAPI] {params} 失败: {type(e).__name__}")
        return []
    if str(data.get("error_code")) != "0":
        log(f"[smzdmAPI] {params} error_code={data.get('error_code')}")
        return []
    return data.get("data", {}).get("rows") or []


def _api_rows_to_items(rows: list[dict], src: str) -> dict[str, dict]:
    items: dict[str, dict] = {}
    for it in rows:
        pid = str(it.get("article_id") or "")
        title = re.sub(r"\s+", " ", str(it.get("article_title") or "")).strip()
        if not pid or not title or pid in items:
            continue
        mm = model_hit(title)
        if not mm:
            continue
        # article_price 形如 "3920元（需用券）" → 取首个数字；0/极小值视为无价
        pm = re.search(r"\d+(?:\.\d+)?", str(it.get("article_price") or ""))
        price = pm.group(0) if pm and float(pm.group(0)) >= 100 else ""
        mk = model_key(mm.group(0))
        url = f"https://www.smzdm.com/p/{pid}/"
        items[pid] = {
            "url": url, "title": title, "price": price,
            "src": src, "kind": "deal",
            "key": f"{mk}|{price}" if price else f"{mk}|{url}",
            "model": mk,
        }
    return items


def fetch_smzdm_api_cat() -> list[dict]:
    out: dict[str, dict] = {}
    for name, cid in SMZDM_API_CATS.items():
        items = _api_rows_to_items(_smzdm_api_rows({"category_id": cid}), "smzdm_cat")
        for pid, it in items.items():
            out.setdefault(pid, it)
        time.sleep(0.4)
    log(f"[smzdm分类API] 命中机型好价 {len(out)} 条（分类 {list(SMZDM_API_CATS)}）")
    return list(out.values())


def fetch_smzdm_api_kw() -> list[dict]:
    out: dict[str, dict] = {}
    for kw in SMZDM_API_KEYWORDS:
        items = _api_rows_to_items(_smzdm_api_rows({"keyword": kw}), "smzdm_kw")
        for pid, it in items.items():
            out.setdefault(pid, it)
        time.sleep(0.4)
    log(f"[smzdm搜索API] 命中机型好价 {len(out)} 条（关键词 {len(SMZDM_API_KEYWORDS)} 组）")
    return list(out.values())


# ──────────────── 源五：中关村在线手机报价（行情参考价） ────────────────

def fetch_zol() -> list[dict]:
    items: dict[str, dict] = {}
    for page_url in ZOL_URLS:
        try:
            r = requests.get(page_url, headers=UA, timeout=25)
            r.raise_for_status()
            page = r.text
        except requests.RequestException as e:
            log(f"[ZOL] {page_url} 抓取失败: {type(e).__name__}")
            continue
        for block in page.split('data-follow-id="p')[1:]:
            pid_m = re.match(r"(\d+)", block)
            tm = re.search(r'alt="([^"]+)"', block)
            pm = re.search(r'<b class="price-type">([\d.]+)</b>', block)
            if not (pid_m and tm and pm):
                continue
            # ZOL 配置写法 "12GB/256GB" → "12GB+256GB"，对齐配置解析
            title = re.sub(r"(\d+)\s*GB\s*/\s*(\d+(?:TB|GB))", r"\1GB+\2",
                           tm.group(1), flags=re.I)
            mm = model_hit(title)
            if not mm:
                continue
            pid = pid_m.group(1)
            if pid in items:
                continue
            href = re.search(r'href="(/cell_phone/index\d+\.shtml)"', block)
            item_url = ("https://detail.zol.com.cn" + href.group(1)) if href else page_url
            price = pm.group(1)
            mk = model_key(mm.group(0))
            items[pid] = {
                "url": item_url, "title": title, "price": price,
                "src": "zol", "kind": "deal",
                "key": f"{mk}|{price}",
                "model": mk,
            }
    out = list(items.values())
    log(f"[ZOL] 命中机型报价 {len(out)} 条")
    return out


# ──────────────────────── 源七：IT之家 RSS ────────────────────────

def fetch_ithome() -> list[dict]:
    r = requests.get(ITHOME_RSS, headers=UA, timeout=25)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not (model_hit(title) and DEAL_WORD_RE.search(title)):
            continue
        if not link.startswith("http"):
            continue
        out.append({"url": link, "title": title, "price": "",
                    "src": "ithome", "kind": "news", "key": f"n|{link}", "model": ""})
    log(f"[ithome] 命中机型促销资讯 {len(out)} 条")
    return out


# ──────────────── 源六：快科技（机型促销资讯） ────────────────

def fetch_mydrivers() -> list[dict]:
    r = requests.get(MYDRIVERS_URL, headers=UA, timeout=25)
    r.raise_for_status()
    # 响应头无 charset、按 latin1 解码会毁掉中文（DEAL_WORD_RE 需要），页内 meta 为 utf-8
    text = r.content.decode("utf-8", errors="ignore")
    pairs = re.findall(
        r'<a[^>]+href="(https://news\.mydrivers\.com/\d+/\d+/\d+\.htm)"[^>]*>\s*'
        r"([^<]{6,100}?)\s*</a>",
        text,
    )
    out, seen = [], set()
    for url, title in pairs:
        if url in seen:
            continue
        seen.add(url)
        title = re.sub(r"\s+", " ", title).strip()
        if not (model_hit(title) and DEAL_WORD_RE.search(title)):
            continue
        out.append({"url": url, "title": title, "price": "",
                    "src": "mydrivers", "kind": "news", "key": f"n|{url}", "model": ""})
    log(f"[快科技] 命中机型促销资讯 {len(out)} 条")
    return out


# ──────────────── 状态：同价 3 次 / 间隔 30 分钟 ────────────────

def load_state() -> dict:
    """返回 {key: {"count": n, "last": iso}}；旧格式/损坏视为首次运行"""
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data.get("entries"), dict):
            return data["entries"]
    except (OSError, ValueError):
        pass
    return {}


def save_state(entries: dict) -> None:
    # 修剪：超期 + 超量
    cutoff = (now_cn() - timedelta(days=STATE_MAX_AGE_DAYS)).isoformat(timespec="seconds")
    kept = {k: v for k, v in entries.items() if v.get("last", "") >= cutoff}
    if len(kept) > STATE_KEEP:
        for k in sorted(kept, key=lambda x: kept[x].get("last", ""))[: len(kept) - STATE_KEEP]:
            del kept[k]
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"entries": kept, "updated": now_cn().isoformat(timespec="seconds")},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def gate(items: list[dict], entries: dict) -> list[dict]:
    """按规则筛出本批可推送条目；准入即刻写入内存状态（防同批重复）"""
    now = now_cn()
    fresh = []
    for it in items:
        e = entries.get(it["key"])
        if it["kind"] == "news":
            if e is None:
                fresh.append(it)
                entries[it["key"]] = {"count": 1, "last": now.isoformat(timespec="seconds")}
        else:
            if e is None:
                fresh.append(it)
                entries[it["key"]] = {"count": 1, "last": now.isoformat(timespec="seconds")}
            elif e["count"] < MAX_SAME_PUSH:
                last = datetime.fromisoformat(e["last"])
                if now - last >= timedelta(minutes=SAME_PUSH_GAP_MIN):
                    e["count"] += 1
                    e["last"] = now.isoformat(timespec="seconds")
                    fresh.append(it)
    return fresh


# ──────────────────────── 消息组装与推送 ────────────────────────

def build_text(deals: list[dict], news: list[dict], batch_info: str = "") -> str:
    now = now_cn().strftime("%m-%d %H:%M")
    total = len(deals) + len(news)
    lines = [
        "━━━━━━━━━━━━━━━━━━",
        f"📱 机型好价速报 · {now} · 新 {total} 条{batch_info}",
        "━━━━━━━━━━━━━━━━━━",
    ]
    if deals:
        lines += ["", f"━━ 好价爆料 · {len(deals)} 条 ━━"]
        for i, d in enumerate(deals[:MAX_PUSH], 1):
            head = f"▸ {i}. {d['title']}"
            if d["price"]:
                head += f"　¥{d['price']}"
            lines += [head, f"　　{d['url']}", ""]
    if news:
        lines += ["", f"━━ 促销资讯 · {len(news)} 条 ━━"]
        for i, n in enumerate(news, 1):
            lines += [f"▸ {i}. {n['title']}", f"　　{n['url']}", ""]
    lines += [
        "━━━━━━━━━━━━━━━━━━",
        "监测：Find X9/X10 · vivo X300/X500 全系列 + OPPO官方直降盯价",
        "好价=低于发行价28% · 同款同价最多3次间隔30分钟 · 全天候巡检",
    ]
    return "\n".join(lines)


def push(token: str, title: str, content: str) -> None:
    """按渠道逐个发送。好价默认只走 clawbot（微信ClawBot），不发微信服务号；
    若配置的渠道全部失败，自动回退微信服务号（wechat）保证消息不丢。
    临时改渠道用环境变量 PUSHPLUS_CHANNELS（如 "wechat,clawbot"）。
    逐渠道独立成败，某渠道未绑定不影响其它渠道。"""
    channels = [c.strip()
                for c in os.environ.get("PUSHPLUS_CHANNELS", "clawbot").split(",")
                if c.strip()]

    def send(ch: str) -> tuple:
        try:
            r = requests.post(
                PUSH_URL,
                json={"token": token, "title": title, "content": content,
                      "template": "txt", "channel": ch},
                timeout=30,
            )
            r.raise_for_status()
            body = r.json()
            if body.get("code") == 200:
                return (ch, True, f"messageid={body.get('data')}")
            return (ch, False, f"code={body.get('code')} {body.get('msg')}")
        except (requests.RequestException, ValueError) as e:
            return (ch, False, f"{type(e).__name__}: {e}")

    results = [send(ch) for ch in channels]
    # 回退：配置渠道全部失败且未显式包含服务号 → 自动补发服务号
    # 回退不记状态，仅对本条生效：下一条仍从 ClawBot 开始尝试
    if not any(ok for _, ok, _ in results) and "wechat" not in channels:
        log("配置渠道全部失败，回退微信服务号（仅本条；下一条仍优先 ClawBot）")
        results.append(send("wechat"))
    for ch, ok, detail in results:
        log(f"推送[{ch}] {'成功' if ok else '失败'} {detail}")
    if not any(ok for _, ok, _ in results):
        raise RuntimeError("所有渠道推送失败（含服务号回退）: " + str(results))


def dedup_news(news: list[dict]) -> list[dict]:
    """跨来源同新闻只播一条：标题去来源前缀/标点取前 14 字，任一互含视为同一新闻"""
    cores, out = [], []
    for n in news:
        core = re.sub(r"^(IT之家|快科技|讯|消息|【|\[)+", "", n["title"])
        core = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", core)[:14]
        if core and any(core in s or s in core for s in cores):
            continue
        cores.append(core)
        out.append(n)
    return out


def plan_batches(deals: list[dict], news: list[dict]) -> tuple[list[list[dict]], list[dict]]:
    """分批：单条消息 ≤MAX_PUSH 条；单轮最多 MAX_MSGS_PER_RUN 批，余量返回给下轮续推"""
    combined = deals + news
    chunks = [combined[i:i + MAX_PUSH] for i in range(0, len(combined), MAX_PUSH)]
    return chunks[:MAX_MSGS_PER_RUN], chunks[MAX_MSGS_PER_RUN:]


# ──────────────────────────── 主流程 ────────────────────────────

def main() -> int:
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    test = "--test" in argv
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()

    deals, news = [], []
    for name, fn, bucket in (("smzdm", fetch_smzdm, deals),
                             ("smzdm榜", fetch_smzdm_top, deals),
                             ("smzdm分类API", fetch_smzdm_api_cat, deals),
                             ("smzdm搜索API", fetch_smzdm_api_kw, deals),
                             ("ZOL", fetch_zol, deals),
                             ("ithome", fetch_ithome, news),
                             ("快科技", fetch_mydrivers, news),
                             ("oppo官方", fetch_oppo_official, deals)):
        try:
            bucket.extend(fn())
        except (requests.RequestException, ET.ParseError) as e:
            log(f"源 {name} 抓取失败: {type(e).__name__} {e}")

    if len(news) > 1:
        news = dedup_news(news)

    # ── 好价判断：低于发行价 28% 才保留，达不到静默淘汰 ──
    if deals:
        kept = []
        for d in deals:
            ok, why = is_good_deal(d)
            if ok:
                kept.append(d)
            else:
                log(f"[好价过滤] 淘汰 {why} | {d['title'][:44]}")
        if len(kept) != len(deals):
            log(f"[好价过滤] {len(kept)}/{len(deals)} 条达标（低于发行价 28%）")
        deals = kept

    if not deals and not news:
        log("两个源都没拿到数据，退出")
        return 1

    entries = load_state()
    first_run = not STATE_PATH.exists() or not entries

    if test:
        combo = (deals + news)[:5]
        deals = [x for x in combo if x["kind"] == "deal"]
        news = [x for x in combo if x["kind"] == "news"]
        for it in combo:   # 测试也记账，避免后续重复推
            entries[it["key"]] = {"count": 1,
                                  "last": now_cn().isoformat(timespec="seconds")}
        log(f"TEST 模式：推送最新 {len(combo)} 条")
    elif first_run and not dry:
        for it in deals + news:
            entries[it["key"]] = {"count": 1,
                                  "last": now_cn().isoformat(timespec="seconds")}
        save_state(entries)
        if LEGACY_PATH.exists():
            LEGACY_PATH.unlink()   # 清掉旧格式文件
        log(f"首次运行，基线记录 {len(deals) + len(news)} 条（不推送）")
        return 0
    else:
        all_items = deals + news
        fresh = gate(all_items, entries)
        deals = [x for x in fresh if x["kind"] == "deal"]
        news = [x for x in fresh if x["kind"] == "news"]

    if not deals and not news:
        log("无新条目（或同款未到 30 分钟间隔）")
        save_state(entries)   # 记录间隔时钟
        return 0

    # ── 分批发货：每条 ≤10 条，单轮 ≤4 条消息；余量撤销记账，下轮巡检自动续推 ──
    batches, overflow = plan_batches(deals, news)
    for it in [x for c in overflow for x in c]:
        entries.pop(it["key"], None)

    if dry:
        print(f"──── DRY RUN · 共 {len(batches)} 批"
              f"（单批≤{MAX_PUSH}条，单轮≤{MAX_MSGS_PER_RUN}批）────")
        for idx, batch in enumerate(batches, 1):
            d = [x for x in batch if x["kind"] == "deal"]
            n = [x for x in batch if x["kind"] == "news"]
            bi = f" · 批 {idx}/{len(batches)}" if len(batches) > 1 else ""
            print(f"──── 批次 {idx}/{len(batches)} ────")
            print(build_text(d, n, bi))
        if overflow:
            print(f"（余 {sum(len(c) for c in overflow)} 条留下一轮巡检续推）")
        return 0

    if not token:
        log("缺少 PUSHPLUS_TOKEN")
        return 1

    sent, failed = 0, 0
    for idx, batch in enumerate(batches, 1):
        d = [x for x in batch if x["kind"] == "deal"]
        n = [x for x in batch if x["kind"] == "news"]
        bi = f" · 批 {idx}/{len(batches)}" if len(batches) > 1 else ""
        title = f"📱【好价】比发行价低 28%+ · 新 {len(batch)} 条{bi}"
        try:
            push(token, title, build_text(d, n, bi))
            sent += 1
        except RuntimeError as e:
            failed += 1
            log(f"批 {idx}/{len(batches)} 推送失败，该批撤销记账下轮重试: {e}")
            for it in batch:
                entries.pop(it["key"], None)
        if idx < len(batches):
            time.sleep(MSG_GAP_SEC)
    if sent == 0 and failed:
        raise RuntimeError(f"全部 {failed} 批推送失败")
    if overflow:
        log(f"余 {sum(len(c) for c in overflow)} 条留下一轮巡检续推")
    save_state(entries)
    log(f"推送 {sent}/{len(batches)} 批 · 状态 {len(entries)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
