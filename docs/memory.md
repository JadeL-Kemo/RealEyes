# RealEyes — Memory architecture

> 目标：让“眼睛”记得发生过什么，并且能被本地模型与用户低成本检索；MVP 保持简单、可检查、全本地。

## 分层

| 层 | 媒介 | 写入方 | 用途 |
|---|---|---|---|
| M0 原始 | `F:\realeyes\events\events.jsonl` | 服务（追加） | 事实来源；一行一个事件 |
| M1 索引 | `F:\realeyes\state\realeyes.db`（SQLite） | 服务 | `events`(id,ts,source,kind,severity,summary,payload) + `digests`(period,ts,path,summary) |
| M2 摘要 | `F:\realeyes\digests\hourly-*.md`、`daily-*.md` | 服务（本地 9B） | 人类可读的时段摘要；也是 M3 的语料 |
| M3 检索 | `memory.recall()` | 按需 | 关键词 + 时间窗，按时间倒序；CLI `--recall` |

事件 schema 见 `docs/SPEC.md` §3。

## 语义

- **只追加**：M0/M1 不修改历史事件；删除只由保留策略按天清理（`storage.keep_days`，默认 14 天，且受磁盘下限保护）。
- **digest 是派生物**：即使删除 digest 也不影响事实；可随时重算。
- **recall = 过滤而非排序优化**：MVP 用 JSONL 顺序扫描 + 时间窗过滤，数据量在 10^5 事件内够用；后续可加 SQLite FTS5 或 embedding。
- **模型可替换**：attention 与 digest 默认都用 `qwen3.5-9b`；`model_pool` 可轮转多个模型；`model_digest` 可切 27B。

## 保留与隐私

- 事件：按天目录 `events/YYYYMMDD/`；超过 `keep_days` 且磁盘低于 `min_free_gb` 时优先清理最旧的关键帧与事件目录。
- 关键帧只在有 VLM 或规则事件时保存；规则 no_vlm 事件默认不存图（省空间）。
- 全本地：默认不把任何事件/图像发往云端；云端 tier 需显式启用并受预算闸门约束。

## 查询示例

```
python realeyes.py --recall 关键词 --hours 3
python realeyes.py --recall file_changed --hours 24
python realeyes.py --recall 黑色 --hours 2
```

输出：时间窗内事件数、命中事件（时间/来源/类型/严重度/摘要）、每条事件首个关键帧路径；若命中 digest，会打印对应 md 片段。
