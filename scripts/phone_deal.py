# coding=utf-8
"""手机好价监控：指定机型优惠 → PushPlus（微信ClawBot，不发服务号）

监测范围（型号白名单）：
- OPPO Find X9 系列：X9 Pro / X9s Pro / X9 Ultra（基础款 Find X9 已停跟）
- OPPO Find X10 系列：Find X10 / X10 Pro Max / X10 E / (未来 Ultra)
- vivo X300 系列：X300 Pro / X300 Ultra（X300 / X300s / X300E 已停跟）
- vivo X500 系列：X500 / X500 Pro / X500 Pro Max
（前缀匹配，未来新增后缀自动覆盖；EXCLUDED_MODELS 里的基础款除外）

推送规则：
- 好价判断：售价须低于官方【发行价】35% 以上才算好价（RRP_TABLE 发行价表），
  达不到直接静默淘汰——官方发行价原价冒充的"好价"一律不推
- 同一条信息只推一次：同一「型号 + 价格档」见过就跳过，不做二次、三次提醒
- 价格按 10 元归档（3441.65/3441.7/3442 算同一档），同型号 24 小时内价格相差 ≤1.5%
  也视为同一条信息——避免不同渠道差几块钱就被当成新降价反复推
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

CN_TZ = timezone(timedelta(hours=8))  # 状态/展示统一北京时间：Actions 跑在 UTC、本地在北京，混写会打乱 15 分钟轮转


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
# ── 去重：同一条信息只推一次（2026-10-26 收紧）──
# 旧规则是「同款同价最多推 3 次、间隔 15 分钟」，等于同一条爆料在半小时内被推三遍。
# 现在改成见过即跳过；真正的价格变动会落到新的价格档，自然成为新条目。
PRICE_BUCKET = 10             # 手机价格归档粒度（元）：3441.65/3441.7/3442 归为同一档
NEAR_PRICE_PCT = 0.01         # 同型号近价判定：相差 ≤1%（按价格比例，不按绝对值）
NEAR_PRICE_FLOOR = 5          # 近价判定的绝对下限（元）：500 元的打印机不该容忍 20 元偏差
NEAR_PRICE_HOURS = 24         # 近价判定的时间窗口（小时），超过就允许重新推
PHONE_MIN_PRICE = 800         # 手机价格下限：低于此值基本是配件/定金/券额被误当整机价
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

# ── 已停止监控的具体机型（精确到型号，不影响同系列 Pro/Ultra 等） ──
# 2026-10-20 按用户要求下线：Find X9 基础款、vivo X300 / X300s / X300E
EXCLUDED_MODELS = {"x9", "x300", "x300s", "x300e"}

# 配件标题一律不进推送（“Find X9 充电器”这类误配）
ACC_RE = re.compile(
    r"充电器|数据线|充电线|手机壳|保护壳|钢化膜|贴膜|耳机|适配器|背夹|"
    r"延保|礼品卡|以旧换新|清洁套装"
)


def model_hit(title: str):
    """机型匹配（排除配件标题与已停跟机型），返回 Match | None"""
    if ACC_RE.search(title):
        return None
    m = MODEL_RE.search(title)
    if not m:
        return None
    if full_model_id(title) in EXCLUDED_MODELS:
        return None
    return m

# ── 打印机监控（2026-10-24 新增）──
# 与手机不同：打印机按「绝对到手价区间」判定，不看发行价折扣
# 用户指定：兄弟 DCP-T435W 400–500 元；爱普生 L335x（L3350–L3359 全系）400–600 元
PRINTER_TARGETS = {
    "brother_t435w": {"name": "兄弟 DCP-T435W", "lo": 400, "hi": 500},
    "epson_l335x":   {"name": "爱普生 L335x",   "lo": 400, "hi": 600},
}
# 三个关键词即可覆盖：DCP-T435W 精确命中兄弟；L3351/L3353 各自返回 60+ 条
# 实测 L3355/L3356 新增很少（且多为 004 墨水），故不加，省请求量
PRINTER_KEYWORDS = ["DCP-T435W", "L3351", "L3353"]
# 耗材/配件标题不进推送（格之格、天威这类墨水标题会带上机型名）
PRINTER_ACC_RE = re.compile(
    r"墨水|墨瓶|墨盒|维护箱|废墨|相纸|打印纸|硒鼓|粉盒|碳粉|"
    r"数据线|电源线|标签纸|兼容|适用|格之格|天威|才进|彩格"
)
# 上一代 L32xx 混排标题：如「L3353/L3355 3351 3251 3253」这种一号多机的标题，
# 低价往往对应 3251/3253，落到我们区间里就是误报，直接丢
PRINTER_OLD_GEN_RE = re.compile(r"(?<!\d)325\d(?!\d)")


def printer_model_id(title: str) -> str:
    """标题 → 打印机型号键；识别不出返回 ''。
    注意 T436W 是另一款，用 T435W(?!\\d) 精确排除。"""
    if PRINTER_ACC_RE.search(title):
        return ""
    if re.search(r"T435W(?!\d)", title, re.I):
        return "brother_t435w"
    if PRINTER_OLD_GEN_RE.search(title):
        return ""
    if re.search(r"L\s?335\d(?!\d)", title, re.I):
        return "epson_l335x"
    return ""


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
    "36878": "OPPO Find X9 Pro",
    "39805": "OPPO Find X9s Pro",
    "39829": "OPPO Find X9 Ultra",
    "45215": "OPPO Find X10",
    "45203": "OPPO Find X10 Pro Max",
    "45222": "OPPO Find X10 E",
}

# ── 好价阈值：低于发行价 35% 才算好价 ──
GOOD_DEAL_DISCOUNT = 0.35        # 好价判定：售价 ≤ 发行价 ×(1-35%)
# 异常下限：手机不可能打到发行价 4.5 折，低于此值基本是券额/配件/定金被误当整机价
MIN_PRICE_RATIO = 0.45

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


def price_note(raw: str, title: str = "") -> str:
    """从价格原文/标题提取价格成立条件，如 券后 / 含国补 / 含PLUS。
    这些条件不写清楚，用户点进去看到的就是原价，会以为价格是假的。"""
    text = f"{raw} {title}"
    notes = []
    if "用券" in text:
        notes.append("券后")
    if "国家补贴" in text or "国补" in text:
        notes.append("含国补")
    if "plus" in text.lower():
        notes.append("含PLUS")
    if "会员" in text:
        notes.append("会员价")
    if "以旧换新" in text or "换新" in text:
        notes.append("含以旧换新")
    return "·".join(notes)


def is_good_deal(item: dict) -> tuple[bool, str]:
    """好价判断：售价 ≤ 发行价×(1-35%) 才算好价。返回 (通过?, 说明)"""
    title = item["title"]
    try:
        price = float(item["price"])
    except (TypeError, ValueError):
        return False, "无价格"
    if price < PHONE_MIN_PRICE:
        return False, f"¥{price:.0f} 低于手机价格下限 ¥{PHONE_MIN_PRICE}，疑似配件/定金"
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
    # 下限：手机不可能打到发行价 4.5 折，低于此值基本是优惠券金额/配件/定金被误当整机价
    if price < rrp * MIN_PRICE_RATIO:
        return False, f"¥{price:.0f} 仅发行价¥{rrp} 的 {price / rrp:.0%}，疑似异常数据"
    limit = rrp * (1 - GOOD_DEAL_DISCOUNT)
    return price <= limit, f"¥{price:.0f}/发行价¥{rrp}，门槛¥{limit:.0f}"


def model_key(match_text: str) -> str:
    """匹配文本 → 归一化型号键：'OPPO Find X9 Pro' → 'findx9'"""
    k = re.sub(r"[^a-z0-9]", "", match_text.lower())
    return k.replace("oppo", "").replace("vivo", "")


def price_bucket(price: str, step: int = PRICE_BUCKET) -> str:
    """价格归一到 step 元档：3441.65 / 3441.7 / 3442 → 都是 3440。

    同一台手机在不同页面/不同时刻抓到的报价常差几毛到几元，
    不归一化就会被当成「新价格」反复推送——这是历史上去重失效的主因。
    """
    try:
        return str(int(round(float(price) / step)) * step)
    except (TypeError, ValueError):
        return price


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
            "key": f"{mk}|{price_bucket(price)}" if price else f"{mk}|{url}",
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
        # 与 smzdm 等源共用「型号+价格档」命名空间：同款同价只推一次，
        # 不再因为「来自官方商城」而单独再推一遍
        hm = model_hit(f"OPPO {detail}")
        mk = model_key(hm.group(0)) if hm else f"off{sid}"
        items.append({
            "url": f"https://www.opposhop.cn/cn/m/product/index?skuId={sid}",
            "title": f"OPPO官方 · {detail}",
            "price": price,
            "price_note": "",          # 官方直售价，无券无补贴
            "mall": "OPPO官方商城",
            "src": "oppo", "kind": "deal",
            "key": f"{mk}|{price_bucket(price)}",
            "model": mk,
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
            if any(k in subtitle for k in ("过期", "已结束", "售罄")):      # 已失效爆料不推
                continue
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
                "price_note": price_note(subtitle, title),
                "mall": "",
                "src": "smzdm_top", "kind": "deal",
                "key": f"{mk}|{price_bucket(price)}",
                "model": mk,
            }
    out = list(items.values())
    log(f"[smzdm榜] 命中机型好价 {len(out)} 条")
    return out


# ──────────────── 源三/四：smzdm 官方 JSON API ────────────────

def _smzdm_api_rows(params: dict, limit: int = 20) -> list[dict]:
    """调 smzdm 官方列表 API，返回 rows；失败返回 []"""
    query = {"limit": limit, "offset": 0, "type": "youhui", "order": "time", **params}
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
        # article_price 形如 "3920元（需用券）" / "过期 | 7154.11元"
        raw = str(it.get("article_price") or "").strip()
        if any(k in raw for k in ("过期", "已结束", "售罄")):   # 失效爆料点进去买不到这个价
            continue
        pm = re.search(r"\d+(?:\.\d+)?", raw)
        price = pm.group(0) if pm and float(pm.group(0)) >= 100 else ""
        mk = model_key(mm.group(0))
        url = f"https://www.smzdm.com/p/{pid}/"
        items[pid] = {
            "url": url, "title": title, "price": price,
            "price_note": price_note(raw, title),
            "mall": str(it.get("article_mall") or ""),
            "src": src, "kind": "deal",
            "key": f"{mk}|{price_bucket(price)}" if price else f"{mk}|{url}",
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


# ──────────────── 源九：打印机好价（绝对价格窗口） ────────────────

def is_printer_deal(item: dict) -> tuple[bool, str]:
    """打印机好价：到手价必须落在该型号的价格窗口内。返回 (通过?, 说明)"""
    tgt = PRINTER_TARGETS.get(item.get("pcat") or "")
    if not tgt:
        return False, "未配置价格窗口"
    try:
        price = float(item["price"])
    except (TypeError, ValueError):
        return False, "无价格"
    lo, hi = tgt["lo"], tgt["hi"]
    if lo <= price <= hi:
        return True, f"{tgt['name']} ¥{price:.0f} 落在 ¥{lo}–{hi}"
    if price > hi:
        return False, f"{tgt['name']} ¥{price:.0f} 高于上限 ¥{hi}"
    return False, f"{tgt['name']} ¥{price:.0f} 低于下限 ¥{lo}"


def fetch_printer_deals() -> list[dict]:
    """打印机爆料：走 smzdm 站内搜索。
    limit 拉到 100，一个关键词可覆盖近一个月历史，过期爆料会被下面的失效过滤掉。"""
    items: dict[str, dict] = {}
    for kw in PRINTER_KEYWORDS:
        rows = _smzdm_api_rows({"keyword": kw}, limit=100)
        for it in rows:
            pid = str(it.get("article_id") or "")
            title = re.sub(r"\s+", " ", str(it.get("article_title") or "")).strip()
            if not pid or not title or pid in items:
                continue
            pcat = printer_model_id(title)
            if not pcat:
                continue
            raw = str(it.get("article_price") or "").strip()
            if any(k in raw for k in ("过期", "已结束", "售罄")):   # 点进去买不到这个价
                continue
            pm = re.search(r"\d+(?:\.\d+)?", raw)
            if not pm:
                continue
            price = pm.group(0)
            if float(price) < 100:
                continue
            items[pid] = {
                "url": f"https://www.smzdm.com/p/{pid}/",
                "title": title,
                "price": price,
                "price_note": price_note(raw, title),
                "mall": str(it.get("article_mall") or ""),
                "src": "smzdm_printer", "kind": "deal",
                "cat": "printer", "pcat": pcat,
                # key 按 5 元分档：1~4 元的抖动不重复打扰，真降价（跨档）才推
                "key": f"{pcat}|{int(float(price) // 5) * 5}",
                "model": pcat,
            }
        time.sleep(0.5)
    log(f"[打印机] 命中 {len(items)} 条爆料（关键词 {len(PRINTER_KEYWORDS)} 组）")
    return list(items.values())


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
                "price_note": "参考价",     # ZOL 给的是媒体参考价，不是电商实际售价
                "mall": "中关村在线",
                "src": "zol", "kind": "deal",
                "key": f"{mk}|{price_bucket(price)}",
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


# ──────────────── 状态：同一条信息只推一次 ────────────────

def _migrate_keys(entries: dict) -> dict:
    """旧版 key 迁移到「型号 + 价格档」。

    归一化之前存下的 key 是 {型号}|{原始价格}（如 findx9pro|3441.65），
    与新格式 {型号}|{3440} 对不上——不迁移的话，上线第一轮会把历史条目
    全部当成「新降价」重推一遍，正好是要解决的问题本身。
    旧 OPPO 命名空间 off|{sid}|{price} 也一并映射回型号。
    """
    out: dict = {}
    for k, v in entries.items():
        if k.startswith("n|"):          # 资讯 key 不涉及价格
            out[k] = v
            continue
        parts = k.split("|")
        if len(parts) == 3 and parts[0] == "off":
            name = OPPO_OFFICIAL_SKUS.get(parts[1], "")
            hm = model_hit(f"OPPO {name}") if name else None
            nk = f"{model_key(hm.group(0))}|{price_bucket(parts[2])}" if hm else k
        elif len(parts) == 2:
            nk = f"{parts[0]}|{price_bucket(parts[1])}"
        else:
            nk = k
        old = out.get(nk)
        if old is None or v.get("last", "") > old.get("last", ""):
            out[nk] = v
    return out


def load_state() -> dict:
    """返回 {key: {"count": n, "last": iso}}；旧格式/损坏视为首次运行"""
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data.get("entries"), dict):
            return _migrate_keys(data["entries"])
    except (OSError, ValueError):
        pass
    return {}


def save_state(entries: dict) -> None:
    """写回状态。

    5 分钟一轮的高频巡检里，绝大多数轮次不会有任何状态变化。若每轮都写文件，
    workflow 就会每轮提交一次 commit（一天 288 次），把仓库历史刷成一片噪音。
    所以先跟磁盘上的内容比对，真的变了才落盘。
    """
    # 修剪：超期 + 超量
    cutoff = (now_cn() - timedelta(days=STATE_MAX_AGE_DAYS)).isoformat(timespec="seconds")
    kept = {k: v for k, v in entries.items() if v.get("last", "") >= cutoff}
    if len(kept) > STATE_KEEP:
        for k in sorted(kept, key=lambda x: kept[x].get("last", ""))[: len(kept) - STATE_KEEP]:
            del kept[k]

    try:
        old = json.loads(STATE_PATH.read_text(encoding="utf-8")).get("entries")
    except (OSError, ValueError):
        old = None
    if old == kept:
        return   # 无实质变化，不落盘（workflow 会看到 no state changes 并跳过 commit）

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"entries": kept, "updated": now_cn().isoformat(timespec="seconds")},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def near_pushed(it: dict, entries: dict, now: datetime) -> bool:
    """同型号、近价、近期推过 → 视为同一条信息。

    价格档（10 元）解决的是同一报价的小数抖动；这里再兜一层：不同渠道/不同卖家
    对同一台机器报价常差几十块，单靠档位仍会漏网，所以对同型号做 ±1.5% 的模糊判重。
    """
    mk = it.get("model") or ""
    try:
        price = float(it["price"])
    except (TypeError, ValueError):
        return False
    if not mk or price <= 0:
        return False
    tol = max(NEAR_PRICE_FLOOR, price * NEAR_PRICE_PCT)
    since = now - timedelta(hours=NEAR_PRICE_HOURS)
    prefix = mk + "|"
    for k, v in entries.items():
        if not k.startswith(prefix):
            continue
        try:
            if datetime.fromisoformat(v.get("last", "")) < since:
                continue
            old = float(k[len(prefix):])
        except (ValueError, TypeError):
            continue
        if abs(old - price) <= tol:
            return True
    return False


def gate(items: list[dict], entries: dict) -> list[dict]:
    """筛出本批可推送条目：同一条信息只推一次。

    见过的 key 直接跳过（不再有「15 分钟后第 2 次、30 分钟后第 3 次」）；
    价格真降了会落到新的价格档，自然作为新条目推送。
    准入即刻写入内存状态，防止同一轮内重复。
    """
    now = now_cn()
    fresh = []
    skipped = 0
    for it in items:
        if it["key"] in entries:
            continue
        if it["kind"] == "deal" and near_pushed(it, entries, now):
            skipped += 1
            continue
        fresh.append(it)
        entries[it["key"]] = {"count": 1, "last": now.isoformat(timespec="seconds")}
    if skipped:
        log(f"[去重] 同型号近价已推过，跳过 {skipped} 条")
    return fresh


# ──────────────────────── 消息组装与推送 ────────────────────────

def build_text(deals: list[dict], news: list[dict], batch_info: str = "") -> str:
    """生成 markdown 正文。用 markdown 而不是纯文本，是因为纯文本里的 URL
    在微信里点不动；markdown 的 [文字](链接) 才是可点击的。"""
    now = now_cn().strftime("%m-%d %H:%M")
    printers = [d for d in deals if d.get("cat") == "printer"]
    phones = [d for d in deals if d.get("cat") != "printer"]
    total = len(deals) + len(news)
    lines = [f"**🔥 好价速报 · {now} · 新 {total} 条{batch_info}**", ""]

    def deal_block(title: str, rows: list[dict]) -> list[str]:
        out = [f"**━━ {title} · {len(rows)} 条 ━━**", ""]
        for i, d in enumerate(rows[:MAX_PUSH], 1):
            out.append(f"**{i}. {d['title']}**")
            meta = []
            if d["price"]:
                p = f"¥{d['price']}"
                if d.get("price_note"):
                    p += f"（{d['price_note']}）"
                meta.append(p)
            if d.get("mall"):
                meta.append(d["mall"])
            meta.append(f"[查看爆料]({d['url']})")
            out += ["　".join(meta), ""]
        return out

    if printers:
        lines += deal_block("🖨️ 打印机好价", printers)
    if phones:
        lines += deal_block("📱 手机好价", phones)
    if news:
        lines += [f"**━━ 促销资讯 · {len(news)} 条 ━━**", ""]
        for i, n in enumerate(news, 1):
            lines += [f"**{i}. {n['title']}**", f"[查看详情]({n['url']})", ""]
    lines += [
        "---",
        "手机：Find X9 Pro/X9s Pro/X9 Ultra · Find X10 系列 · vivo X300 Pro/X300 Ultra/X500 系列（低于发行价 35%）",
        "打印机：兄弟 T435W ¥400–500 · 爱普生 L335x ¥400–600（到手价落入区间才推）",
        "2 分钟一轮巡检 · 价格一变立即推 · 同一条信息只推一次",
    ]
    return "\n".join(lines)


def make_title(batch: list[dict], bi: str = "") -> str:
    """标题随内容变：只有打印机就写打印机，混合写综合，避免手机上看到“手机好价”却是打印机"""
    has_printer = any(x.get("cat") == "printer" for x in batch)
    has_phone = any(x.get("cat") != "printer" and x["kind"] == "deal" for x in batch)
    if has_printer and not has_phone:
        return f"🖨️【打印机好价】新 {len(batch)} 条{bi}"
    if has_printer and has_phone:
        return f"🔥【好价】手机+打印机 · 新 {len(batch)} 条{bi}"
    return f"📱【好价】比发行价低 35%+ · 新 {len(batch)} 条{bi}"


def push(token: str, title: str, content: str) -> None:
    """按渠道逐个发送。默认走微信服务号（wechat），这是已验证能收到消息的渠道；
    若微信服务号失败，再回退 ClawBot。临时改渠道用环境变量 PUSHPLUS_CHANNELS
    （如 "wechat,clawbot"）。逐渠道独立成败，某渠道未绑定不影响其它渠道。"""
    channels = [c.strip()
                for c in os.environ.get("PUSHPLUS_CHANNELS", "wechat").split(",")
                if c.strip()]

    def send(ch: str) -> tuple:
        try:
            r = requests.post(
                PUSH_URL,
                json={"token": token, "title": title, "content": content,
                      "template": "markdown", "channel": ch},
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
    # 回退：配置渠道全部失败且未显式包含 ClawBot → 自动补发 ClawBot
    # 回退不记状态，仅对本条生效：下一条仍从微信服务号开始尝试
    if not any(ok for _, ok, _ in results) and "clawbot" not in channels:
        log("微信服务号失败，回退 ClawBot（仅本条；下一条仍优先服务号）")
        results.append(send("clawbot"))
    for ch, ok, detail in results:
        log(f"推送[{ch}] {'成功' if ok else '失败'} {detail}")
    if not any(ok for _, ok, _ in results):
        raise RuntimeError("所有渠道推送失败（含 ClawBot 回退）: " + str(results))


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
                             ("打印机", fetch_printer_deals, deals),
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

    # ── 好价判断：手机看「低于发行价 35%」，打印机看「到手价落入指定区间」，
    #    达不到静默淘汰。两套口径并存，按 cat 分流 ──
    if deals:
        kept, n_phone, n_print = [], 0, 0
        for d in deals:
            is_print = d.get("cat") == "printer"
            ok, why = is_printer_deal(d) if is_print else is_good_deal(d)
            if ok:
                kept.append(d)
                if is_print:
                    n_print += 1
                else:
                    n_phone += 1
            else:
                log(f"[好价过滤] 淘汰 {why} | {d['title'][:44]}")
        if len(kept) != len(deals):
            log(f"[好价过滤] {len(kept)}/{len(deals)} 条达标"
                f"（手机 {n_phone} 条低于发行价 35% · 打印机 {n_print} 条落入价格区间）")
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
        log("无新条目（同一型号+价格档只推一次，价格真降才会再推）")
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
        title = make_title(batch, bi)
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
