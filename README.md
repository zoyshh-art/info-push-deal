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

## 巡逻节奏

**全天 24 小时不间断，每 5 分钟扫一轮。**

实现方式是「每小时触发一次 job，job 内部自己循环 12 轮、每轮间隔 5 分钟」，而不是直接写
`cron: "*/5 * * * *"`。原因是实测发现 GitHub 对高频 cron 会降级调度——配置写 5 分钟，
实际只跑了 30 分钟一次、中间 20 分钟完全空转；而每小时一次的频率 GitHub 调度是可靠的。
把 5 分钟粒度放进 job 内部，就不受平台定时精度影响了。

上一轮还没跑完时下一次触发会在队列里等待，等它结束立刻接上，所以实际是连续覆盖、没有空档。

工作流也可以手动触发：**Actions → 手机好价监控 → Run workflow**，
勾选 `dry_run` 只打印不推送，`loops` 填轮数（默认 12 轮 = 60 分钟，调试填 1 就行）。

> 高频轮询下绝大多数轮次没有任何状态变化。脚本会在落盘前比对内容，**没有实际变化就不写文件**，
> 工作流看到 `no state changes` 会直接跳过 commit——否则一天几百轮会刷出几百个空 commit。
> 每轮结束都会把状态提交掉，万一长 job 中途被取消，已推送的记录也不会丢、不会重复推。

## 本地运行

```bash
pip install -r requirements.txt
export PUSHPLUS_TOKEN=你的令牌      # Windows: set PUSHPLUS_TOKEN=...
python scripts/phone_deal.py --dry-run   # 只打印
python scripts/phone_deal.py             # 真实推送
```
