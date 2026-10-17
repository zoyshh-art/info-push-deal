# 手机好价监控

从 [info-push](https://github.com/zoyshh-art/info-push) 拆分出来的独立盯价仓库。

**为什么单独开一个公开仓库**：GitHub Free 的私有仓库只有 2000 分钟/月 Actions 额度，按 job 向上取整计费。
好价监控单次约 1 分钟，要高频盯价就必须单独放在**公开仓库**——公开仓库的 Actions 分钟数不计费，
可以做到 **5 分钟一轮**（GitHub 定时任务的最短间隔），比原来私有仓库里的 30 分钟一轮快 6 倍。

## 监控范围

| 来源 | 抓什么 |
|---|---|
| 什么值得买（榜单 / 分类 API / 关键词搜索 / 首页） | 手机好价爆料 |
| ZOL 中关村在线 | 手机降价行情 |
| IT之家 | 手机促销资讯 |
| 快科技 | 促销资讯 |
| OPPO 官方商城 | 7 款目标机型直降 |

型号白名单：OPPO Find X9 / X10 系列、vivo X300 / X500 系列（前缀匹配，含未来后缀）。

## 推送规则

- **低于发行价 28% 才推**，达不到的静默淘汰，不打扰
- 同一「型号 + 价格」最多推 **3 次**
- 同款两次推送最小间隔 **30 分钟**
- 单条消息 ≤10 条，单轮 ≤4 条消息，超出部分下一轮巡检续推
- 首次运行只记基线不推送（避免一上线就刷屏）

节流状态存在 `data/phone_deal_state.json`，由工作流自动提交回仓库。

## 部署

在仓库 `Settings → Secrets and variables → Actions` 添加：

| Secret | 说明 |
|---|---|
| `PUSHPLUS_TOKEN` | PushPlus 令牌，微信接收推送 |

推送渠道默认 `clawbot`（微信 ClawBot）。如需服务号渠道，在工作流里加环境变量
`PUSHPLUS_CHANNELS: "wechat,clawbot"`。

工作流 `.github/workflows/deal-watch.yml` 每 5 分钟自动跑一轮，也可以在
**Actions → 手机好价监控 → Run workflow** 手动触发，勾选 `dry_run` 只打印不推送。

## 本地运行

```bash
pip install -r requirements.txt
export PUSHPLUS_TOKEN=你的令牌      # Windows: set PUSHPLUS_TOKEN=...
python scripts/phone_deal.py --dry-run   # 只打印
python scripts/phone_deal.py             # 真实推送
```
