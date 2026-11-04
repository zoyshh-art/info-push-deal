# coding=utf-8
"""AI 免费额度 / 平台活动监控 → PushPlus

只推「哪里能白嫖」和「哪家活动变了」，不推模型降价、不发教程水文。

三个源，按信号价值排序：

1. ★ rockbenben/free-llm-intel · llm-intel-changelog.md
   每天巡检 70 家厂商（OpenAI / Anthropic / Gemini / Groq / Mistral / DeepSeek /
   通义 / Kimi / 智谱 / 豆包 / 硅基流动 …）的官方博客与定价页，把「免费额度事实变化」
   写成带前值→后值的中文摘要。这是唯一能持续产出「最新免费 token / 活动」的源，
   而且是官方来源核实过的，不是社区传言。
   配套 llm-intel.yaml 提供每家厂商的官方 homepage。

2. GitHub · mnfst/awesome-free-llm-apis · data.json
   「永久免费 LLM API」清单，12k+ star。结构化数据，监控新增服务商 / 新增模型 /
   额度描述变化。变化慢，但一旦变就是长期可用的新免费层。

3. OpenRouter · /api/v1/models
   453 个模型实时价格，只取「完全免费」（输入输出单价都为 0）的模型增减。

刻意不做：
- 模型降价。用户明确说这类信息没价值——而且 OpenRouter 是聚合器，价格随上游
  路由分钟级抖动（实测 gpt-oss-120b 在 $0.113 和 $0.75 之间来回跳），噪音远大于信号。
- 国内「API 中转站」「号池」。2026 年多地警方以非法获取计算机信息系统数据立案，
  公开报道有站长被刑拘；账号随时封禁、数据经第三方转发。只监控厂商官方渠道。
- HN / Reddit 关键词搜索。实测噪音极大（搜 free API credits 返回的全是无关 Show HN）。

推送规则：
- 只推新出现的变化，同一 key 推过就不再推
- intel 源按「厂商 + 日期」去重，同一天同一家最多一条，避免反复修订刷屏
- 首次接入 intel 源时只记基线，不把 150 条历史一次性倒出来

环境变量：PUSHPLUS_TOKEN（必填）
          PUSHPLUS_CHANNELS（默认 wechat）
用法：python scripts/ai_deal.py [--dry-run | --test]
"""

import base64
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
INTEL_REPO = "rockbenben/free-llm-intel"
MNFST_REPO = "mnfst/awesome-free-llm-apis"
OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"

STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "ai_deal_state.json"
SCHEMA = 2                     # 2 = 接入 intel 源，旧状态自动补基线

PUSH_URL = "https://www.pushplus.plus/send"
MAX_PUSH = 12
STATE_KEEP = 3000
SUMMARY_MAX = 150              # 摘要截断长度（中文字符）

# 首次接入 intel 源时打这个标记，避免把历史 150 条一次性推出去
INTEL_FLAG = "meta|intel_baselined"

HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "ai-deal-watch"}


# ──────────────────────────── 状态 ────────────────────────────

def load_state() -> dict:
    try:
        d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if d.get("schema") == SCHEMA and isinstance(d.get("entries"), dict):
            return d["entries"]
    except (OSError, ValueError):
        pass
    return {}


def save_state(entries: dict) -> None:
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


def gh_file(repo: str, path: str) -> str:
    """读 GitHub 仓库文件正文（raw.githubusercontent 本地不通，必须走 contents API）"""
    r = requests.get(f"{GH_API}/repos/{repo}/contents/{path}",
                     headers=HEADERS, timeout=40)
    r.raise_for_status()
    j = r.json()
    if "content" not in j:
        raise RuntimeError(f"{path} 过大，contents API 未返回正文")
    return base64.b64decode(j["content"]).decode("utf-8")


# ──────────────────── 源一：免费政策变化（主源） ────────────────────

def parse_vendors(yaml_txt: str) -> dict:
    """从 llm-intel.yaml 提取 id → {brand, homepage}。
    结构固定（- id / brand / products / homepage），手写状态机比引 PyYAML 省事。"""
    out, cur = {}, {}
    for line in yaml_txt.splitlines():
        s = line.strip()
        if s.startswith("- id:"):
            if cur.get("id"):
                out[cur["id"]] = cur
            cur = {"id": s.split(":", 1)[1].strip()}
        elif s.startswith("brand:") and cur:
            cur["brand"] = s.split(":", 1)[1].strip().strip('"')
        elif s.startswith("homepage:") and cur:
            cur["homepage"] = s.split(":", 1)[1].strip().strip('"')
    if cur.get("id"):
        out[cur["id"]] = cur
    return out


def parse_changelog(md: str) -> list[dict]:
    """解析「## 日期 / ### 厂商（id） / - 摘要：…」结构。

    同一天同一厂商可能有多段（多次修订），只保留最后一段——那是最终结论。
    """
    recs: dict[tuple, dict] = {}
    date = prov = prov_id = summary = None

    def flush():
        if date and prov_id and summary:
            recs[(prov_id, date)] = {"date": date, "id": prov_id,
                                     "name": prov, "summary": summary}

    for line in md.splitlines():
        s = line.rstrip()
        if s.startswith("## "):
            flush()
            date = s[3:].strip()
            prov = prov_id = summary = None
        elif s.startswith("### "):
            flush()
            summary = None
            head = s[4:].strip()
            m = re.match(r"(.+?)（`([^`]+)`）", head)
            if m:
                prov, prov_id = m.group(1).strip(), m.group(2).strip()
            else:
                prov, prov_id = head, re.sub(r"\s+", "_", head.lower())
        elif re.match(r"-\s*摘要[:：]", s):
            # 只剥掉「- 摘要：」这个前缀，不能按冒号 split —— 摘要正文里有
            # 「2026-11-01 00:00」这种时间，按英文冒号切会把后半截吃掉
            summary = re.sub(r"^-\s*摘要[:：]\s*", "", s).strip()
    flush()

    rows = sorted(recs.values(), key=lambda r: r["date"], reverse=True)
    return rows


def parse_intel(rows: list[dict], vendors: dict, entries: dict, now: datetime) -> list[dict]:
    """厂商-天 去重：同一天同一家只推一条"""
    # 首次接入：只记基线，不倒历史
    if INTEL_FLAG not in entries:
        for r in rows:
            mark(entries, f"intel|{r['id']}|{r['date']}", now)
        mark(entries, INTEL_FLAG, now)
        log(f"[intel] 首次接入，记基线 {len(rows)} 条（不推送）")
        return []

    out = []
    for r in rows:
        k = f"intel|{r['id']}|{r['date']}"
        if k in entries:
            continue
        mark(entries, k, now)
        v = vendors.get(r["id"], {})
        text = r["summary"]
        if len(text) > SUMMARY_MAX:
            text = text[:SUMMARY_MAX].rstrip() + "…"
        out.append({
            "kind": "policy", "key": k,
            "title": f"{v.get('brand') or r['name']} · {r['date']}",
            "meta": text,
            "url": v.get("homepage", ""),
        })
    return out


# ──────────────────── 源二：永久免费层清单 ────────────────────

def quota_hash(provider: dict) -> str:
    raw = f"{provider.get('description', '')}|{provider.get('baseUrl', '')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def parse_mnfst(data: dict, entries: dict, now: datetime) -> list[dict]:
    out = []
    for p in data.get("providers") or []:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        flag, country = p.get("flag") or "", p.get("country") or ""
        link = p.get("url") or ""

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


# ──────────────────── 源三：OpenRouter 免费模型 ────────────────────

def _price_per_million(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return -1.0
    if f < 0:                     # -1 表示动态定价
        return -1.0
    return round(f * 1_000_000, 4)


def parse_openrouter(models: list[dict], entries: dict, now: datetime) -> list[dict]:
    out = []
    for m in models:
        mid = (m.get("id") or "").strip()
        if not mid:
            continue
        pricing = m.get("pricing") or {}
        pin = _price_per_million(pricing.get("prompt"))
        pout = _price_per_million(pricing.get("completion"))
        if pin != 0 or pout != 0:
            continue
        k = f"or|free|{mid}"
        if k in entries:
            continue
        mark(entries, k, now)
        ctx = m.get("context_length") or 0
        out.append({
            "kind": "free", "key": k,
            "title": f"OpenRouter 免费模型：{mid}",
            "meta": f"上下文 {ctx // 1000}K · 输入输出均 $0",
            "url": f"https://openrouter.ai/{mid}",
        })
    return out


# ──────────────────────────── 推送 ────────────────────────────

SECTION = {
    "policy":   "📢 免费政策变化",
    "provider": "🎁 新增免费服务商",
    "quota":    "📈 免费额度变化",
    "model":    "🆕 免费清单新增模型",
    "free":     "🆓 OpenRouter 免费模型",
}
ORDER = ["policy", "provider", "quota", "model", "free"]


def build_text(items: list[dict]) -> str:
    now = now_cn().strftime("%m-%d %H:%M")
    lines = [f"**🤖 AI 免费额度 / 活动 · {now} · 新 {len(items)} 条**", ""]
    for kind in ORDER:
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
                meta.append(f"[官网]({x['url']})")
            lines += ["　".join(meta), ""]
    lines += [
        "---",
        "来源：free-llm-intel（每天巡检 70 家厂商官方页）· awesome-free-llm-apis · OpenRouter",
        "只推厂商官方免费额度与政策变化，不涉及第三方转售/号池",
        "每小时巡检 · 同一条变化只推一次",
    ]
    return "\n".join(lines)


def make_title(items: list[dict]) -> str:
    n_policy = sum(1 for x in items if x["kind"] in ("policy", "quota"))
    n_new = len(items) - n_policy
    if n_policy and not n_new:
        return f"📢【AI 免费政策】新 {n_policy} 条变化"
    if n_new and not n_policy:
        return f"🎁【AI 免费额度】新 {n_new} 条"
    return f"🤖【AI 羊毛】政策 {n_policy} · 新增 {n_new}"


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

    # 源一：免费政策变化
    try:
        md = gh_file(INTEL_REPO, "llm-intel-changelog.md")
        vendors = parse_vendors(gh_file(INTEL_REPO, "llm-intel.yaml"))
        rows = parse_changelog(md)
        log(f"[intel] {len(rows)} 条厂商-天记录 · {len(vendors)} 家厂商档案")
        items += parse_intel(rows, vendors, entries, now)
    except (requests.RequestException, ValueError, RuntimeError) as e:
        failed += 1
        log(f"[intel] 抓取失败: {type(e).__name__} {e}")

    # 源二：永久免费层清单
    try:
        data = json.loads(gh_file(MNFST_REPO, "data.json"))
        log(f"[mnfst] {len(data.get('providers') or [])} 个服务商 · "
            f"清单更新于 {data.get('lastUpdated', '?')}")
        items += parse_mnfst(data, entries, now)
    except (requests.RequestException, ValueError, RuntimeError) as e:
        failed += 1
        log(f"[mnfst] 抓取失败: {type(e).__name__} {e}")

    # 源三：OpenRouter 免费模型
    try:
        r = requests.get(OPENROUTER_MODELS, timeout=40)
        r.raise_for_status()
        models = r.json().get("data") or []
        log(f"[openrouter] {len(models)} 个模型")
        items += parse_openrouter(models, entries, now)
    except (requests.RequestException, ValueError) as e:
        failed += 1
        log(f"[openrouter] 抓取失败: {type(e).__name__} {e}")

    if failed == 3:
        log("三个源都失败，退出（疑似网络/接口异常）")
        return 1

    if first_run:
        save_state(entries)
        log(f"首次运行，基线记录 {len(entries)} 条（不推送）")
        return 0

    if not items:
        save_state(entries)
        log("没有新变化")
        return 0

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
