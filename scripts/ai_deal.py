# coding=utf-8
"""AI 免费额度 / 好价监控 → PushPlus

盯两个结构化源（都不需要抓网页、不依赖国内可达性）：

1. GitHub · mnfst/awesome-free-llm-apis
   「永久免费 LLM API」清单，12k+ star，维护活跃（2026-11 仍在更新）。
   仓库根目录的 data.json 是结构化数据，比 README 可靠得多：
     { lastUpdated, providers: [{name, country, flag, url, description, models: [...]}] }
   监控三类变化：新增服务商、新增模型、免费额度/限流描述变化。

2. OpenRouter · /api/v1/models
   453 个模型的实时价格。监控两类变化：
   - 新增完全免费模型（prompt 与 completion 单价都为 0）
   - 主流厂商模型降价 ≥25%

为什么只盯这两个：
- 都是机器可读的结构化数据，不靠解析网页，不会因为改版就失效
- 信号密度高：HN / Reddit 关键词搜索实测噪音极大（全是无关 Show HN），不做
- LiteLLM 价格库文件超 1MB，GitHub contents API 取不到正文，且含大量冷门模型，噪音大

刻意不碰的东西：国内「API 中转站」「号池」。这类按转售价兜售官方额度的生意，
2026 年已被多地警方以非法获取计算机信息系统数据立案（有站长被刑拘的公开报道），
且账号随时封禁、数据经第三方转发。本脚本只监控**厂商官方**的免费层与公开定价。

推送规则：
- 只推「新出现」的信息，同一个 key 推过就不再推（这类信息没有"降价抢购"时效性）
- 首次运行只记基线不推送
- 免费额度描述变化按内容哈希判定，改一个字也算新事件

环境变量：PUSHPLUS_TOKEN（必填）
          PUSHPLUS_CHANNELS（默认 wechat）
用法：python scripts/ai_deal.py [--dry-run | --test]
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

CN_TZ = timezone(timedelta(hours=8))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


def now_cn() -> datetime:
    return datetime.now(CN_TZ).replace(tzinfo=None)


def log(msg: str) -> None:
    print(f"[{now_cn().strftime('%H:%M:%S')}] {msg}", flush=True)


# ──────────────────────────── 配置 ────────────────────────────

GH_API = "https://api.github.com"
MNFST_REPO = "mnfst/awesome-free-llm-apis"
MNFST_FILE = "data.json"
OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"

STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "ai_deal_state.json"
SCHEMA = 1

PUSH_URL = "https://www.pushplus.plus/send"
MAX_PUSH = 12                  # 单条消息最多条目数
STATE_KEEP = 3000              # 状态保留条数

# 降价只盯主流厂商，453 个模型里有大量冷门微调版本，全盯会变成噪音
MAJOR_PREFIXES = (
    "openai/", "anthropic/", "google/", "deepseek/", "qwen/", "meta-llama/",
    "mistralai/", "x-ai/", "moonshotai/", "z-ai/", "minimax/", "nvidia/",
    "cohere/", "amazon/", "microsoft/",
)
PRICE_DROP_RATIO = 0.25        # 降价 ≥25% 才算"划算"，小幅波动不打扰
MIN_OLD_PRICE = 0.05           # 旧价低于 $0.05/百万 token 的不看（已经很便宜，降 25% 无感）

HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "ai-deal-watch"}


# ──────────────────────────── 状态 ────────────────────────────

def load_state() -> dict:
    """返回 {"entries": {key: {"last": iso}}}；损坏或旧版本视为首次运行"""
    try:
        d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if d.get("schema") == SCHEMA and isinstance(d.get("entries"), dict):
            return d["entries"]
    except (OSError, ValueError):
        pass
    return {}


def save_state(entries: dict) -> None:
    # 只保留最近 STATE_KEEP 条，防止无限增长
    kept = dict(sorted(entries.items(),
                       key=lambda kv: kv[1].get("last", ""), reverse=True)[:STATE_KEEP])
    try:
        old = json.loads(STATE_PATH.read_text(encoding="utf-8")).get("entries")
    except (OSError, ValueError):
        old = None
    if old == kept:
        return                      # 无变化不落盘，workflow 看到 no changes 就跳过 commit

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"schema": SCHEMA, "entries": kept,
                    "updated": now_cn().isoformat(timespec="seconds")},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def mark(entries: dict, key: str, now: datetime) -> None:
    entries[key] = {"last": now.isoformat(timespec="seconds")}


# ──────────────────────────── 源一：免费额度清单 ────────────────────────────

def fetch_mnfst() -> dict:
    """读 mnfst 的 data.json（走 api.github.com，raw.githubusercontent 本地不通）"""
    url = f"{GH_API}/repos/{MNFST_REPO}/contents/{MNFST_FILE}"
    r = requests.get(url, headers=HEADERS, timeout=40)
    r.raise_for_status()
    j = r.json()
    if "content" not in j:
        raise RuntimeError("contents API 未返回正文（文件可能过大）")
    import base64
    return json.loads(base64.b64decode(j["content"]).decode("utf-8"))


def quota_hash(provider: dict) -> str:
    """额度描述指纹：description 里写着限流/额度，改了就是新事件"""
    raw = f"{provider.get('description', '')}|{provider.get('baseUrl', '')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def parse_mnfst(data: dict, entries: dict, now: datetime) -> list[dict]:
    out = []
    for p in data.get("providers") or []:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        flag = p.get("flag") or ""
        country = p.get("country") or ""
        link = p.get("url") or ""

        # 1) 新服务商
        pk = f"mnfst|provider|{name}"
        if pk not in entries:
            mark(entries, pk, now)
            out.append({
                "kind": "provider", "key": pk,
                "title": f"新增免费服务商：{name}",
                "meta": f"{flag} {country} · {p.get('description', '')[:90]}",
                "url": link,
            })
            # 新服务商不逐个报模型，避免一次刷十几条
            for m in p.get("models") or []:
                if m.get("id"):
                    mark(entries, f"mnfst|model|{name}|{m['id']}", now)
            mark(entries, f"mnfst|quota|{name}|{quota_hash(p)}", now)
            continue

        # 2) 新增模型
        for m in p.get("models") or []:
            mid = (m.get("id") or "").strip()
            if not mid:
                continue
            mk = f"mnfst|model|{name}|{mid}"
            if mk not in entries:
                mark(entries, mk, now)
                out.append({
                    "kind": "model", "key": mk,
                    "title": f"{name} 新增 {mid}",
                    "meta": f"{flag} 上下文 {m.get('context', '?')} · "
                            f"{m.get('rateLimit', '') or m.get('modality', '')}",
                    "url": link,
                })

        # 3) 额度/限流描述变化
        qk = f"mnfst|quota|{name}|{quota_hash(p)}"
        if not any(k.startswith(f"mnfst|quota|{name}|") for k in entries):
            mark(entries, qk, now)
        elif qk not in entries:
            mark(entries, qk, now)
            out.append({
                "kind": "quota", "key": qk,
                "title": f"{name} 免费额度有变化",
                "meta": f"{flag} {p.get('description', '')[:110]}",
                "url": link,
            })
    return out


# ──────────────────────────── 源二：OpenRouter ────────────────────────────

def _price_per_million(v) -> float:
    """OpenRouter 的 pricing 是 $/token 字符串 → 换算成 $/百万 token"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return -1.0
    if f < 0:                     # -1 表示动态定价，跳过
        return -1.0
    return round(f * 1_000_000, 4)


def fetch_openrouter() -> list[dict]:
    r = requests.get(OPENROUTER_MODELS, timeout=40)
    r.raise_for_status()
    return r.json().get("data") or []


def parse_openrouter(models: list[dict], entries: dict, now: datetime) -> list[dict]:
    out = []
    for m in models:
        mid = (m.get("id") or "").strip()
        if not mid:
            continue
        pricing = m.get("pricing") or {}
        pin = _price_per_million(pricing.get("prompt"))
        pout = _price_per_million(pricing.get("completion"))
        if pin < 0 or pout < 0:
            continue

        # 1) 新增免费模型
        fk = f"or|free|{mid}"
        if pin == 0 and pout == 0:
            if fk not in entries:
                mark(entries, fk, now)
                ctx = m.get("context_length") or 0
                out.append({
                    "kind": "free", "key": fk,
                    "title": f"OpenRouter 免费模型：{mid}",
                    "meta": f"上下文 {ctx // 1000}K · 输入输出均 $0",
                    "url": f"https://openrouter.ai/{mid}",
                })
            continue

        # 2) 主流模型降价
        if not mid.startswith(MAJOR_PREFIXES):
            continue
        cur = pin + pout
        if cur <= 0 or cur < MIN_OLD_PRICE:
            continue
        # 用「已记录的旧价」比对：key 里带价格，所以降价会生成新 key
        old = None
        for k, v in entries.items():
            if k.startswith(f"or|price|{mid}|"):
                try:
                    old = float(k.rsplit("|", 1)[1])
                except ValueError:
                    continue
                break
        pk = f"or|price|{mid}|{cur}"
        if pk in entries:
            continue
        mark(entries, pk, now)
        if old is None:
            continue              # 首次见到这个模型，只记基线
        if cur <= old * (1 - PRICE_DROP_RATIO):
            out.append({
                "kind": "drop", "key": pk,
                "title": f"{mid} 降价 {(1 - cur / old) * 100:.0f}%",
                "meta": f"${old:g} → ${cur:g} / 百万 token（输入+输出合计）",
                "url": f"https://openrouter.ai/{mid}",
            })
    return out


# ──────────────────────────── 推送 ────────────────────────────

SECTION = {
    "provider": "🎁 新增免费服务商",
    "quota":    "📈 免费额度变化",
    "model":    "🆕 免费清单新增模型",
    "free":     "🆓 OpenRouter 免费模型",
    "drop":     "💰 价格下调",
}


def build_text(items: list[dict]) -> str:
    now = now_cn().strftime("%m-%d %H:%M")
    lines = [f"**🤖 AI 免费额度 / 好价 · {now} · 新 {len(items)} 条**", ""]
    order = ["provider", "quota", "free", "model", "drop"]
    for kind in order:
        rows = [x for x in items if x["kind"] == kind]
        if not rows:
            continue
        lines += [f"**━━ {SECTION[kind]} · {len(rows)} 条 ━━**", ""]
        for i, x in enumerate(rows[:MAX_PUSH], 1):
            lines.append(f"**{i}. {x['title']}**")
            meta = []
            if x.get("meta"):
                meta.append(x["meta"])
            if x.get("url"):
                meta.append(f"[领取/查看]({x['url']})")
            lines += ["　".join(meta), ""]
    lines += [
        "---",
        "来源：mnfst/awesome-free-llm-apis（永久免费层清单）· OpenRouter 实时定价",
        "只推厂商官方免费层与公开降价，不涉及任何第三方转售/号池",
        "每小时巡检一次 · 同类信息只推一次",
    ]
    return "\n".join(lines)


def make_title(items: list[dict]) -> str:
    n_free = sum(1 for x in items if x["kind"] in ("provider", "free", "model"))
    n_drop = sum(1 for x in items if x["kind"] == "drop")
    if n_drop and not n_free:
        return f"💰【AI 降价】新 {len(items)} 条"
    if n_free and not n_drop:
        return f"🎁【AI 免费额度】新 {len(items)} 条"
    return f"🤖【AI 羊毛】免费 {n_free} · 降价 {n_drop}"


def push(token: str, title: str, content: str) -> None:
    channels = [c.strip()
                for c in os.environ.get("PUSHPLUS_CHANNELS", "wechat").split(",")
                if c.strip()]
    errs = []
    for ch in channels:
        try:
            r = requests.post(PUSH_URL, json={
                "token": token, "title": title, "content": content,
                "template": "markdown", "channel": ch,
            }, timeout=30)
            j = r.json()
            if j.get("code") == 200:
                log(f"推送成功（{ch}）")
                return
            errs.append(f"{ch}: code={j.get('code')} {j.get('msg')}")
        except (requests.RequestException, ValueError) as e:
            errs.append(f"{ch}: {type(e).__name__}")
    raise RuntimeError("; ".join(errs))


# ──────────────────────────── 主流程 ────────────────────────────

def main() -> int:
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    test = "--test" in argv
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()

    entries = load_state()
    first_run = not entries
    now = now_cn()

    items: list[dict] = []
    failed = 0

    try:
        data = fetch_mnfst()
        log(f"[mnfst] {len(data.get('providers') or [])} 个服务商 · "
            f"清单更新于 {data.get('lastUpdated', '?')}")
        items += parse_mnfst(data, entries, now)
    except (requests.RequestException, ValueError, RuntimeError) as e:
        failed += 1
        log(f"[mnfst] 抓取失败: {type(e).__name__} {e}")

    try:
        models = fetch_openrouter()
        free_n = sum(1 for m in models
                     if _price_per_million((m.get("pricing") or {}).get("prompt")) == 0
                     and _price_per_million((m.get("pricing") or {}).get("completion")) == 0)
        log(f"[openrouter] {len(models)} 个模型 · 其中完全免费 {free_n} 个")
        items += parse_openrouter(models, entries, now)
    except (requests.RequestException, ValueError) as e:
        failed += 1
        log(f"[openrouter] 抓取失败: {type(e).__name__} {e}")

    if failed == 2:
        log("两个源都失败，退出（疑似网络/接口异常）")
        return 1

    if first_run:
        save_state(entries)
        log(f"首次运行，基线记录 {len(entries)} 条（不推送）")
        return 0

    if not items:
        save_state(entries)
        log("没有新条目")
        return 0

    # 同一批内按 key 去重
    seen, uniq = set(), []
    for x in items:
        if x["key"] in seen:
            continue
        seen.add(x["key"])
        uniq.append(x)
    items = uniq

    if test:
        items = items[:5]
        log(f"TEST 模式：推送前 {len(items)} 条")

    if dry:
        print("──── DRY RUN ────")
        print(build_text(items))
        print(f"──── 标题：{make_title(items)} ────")
        return 0

    if not token:
        log("缺少 PUSHPLUS_TOKEN")
        return 1

    title = make_title(items)
    try:
        push(token, title, build_text(items))
    except RuntimeError as e:
        log(f"推送失败，撤销记账下轮重试: {e}")
        for x in items:
            entries.pop(x["key"], None)
        save_state(entries)
        return 1

    save_state(entries)
    log(f"已推送 {len(items)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
