# RealEyes — Specification (vertical slice v0.2)

> 状态：可运行垂直切片。权威代码在 `H:\AgentWorkspace\RealEyes\`；`F:\scripts\realeyes\` 是上一版单文件 MVP 的部署副本（仍在运行，切换见 PLAN.md）。
> 原则：local-first、零 API token；不追求细节，优先核心功能可靠（用户 2026-11-19 指示）。

## 1. 目标与范围

RealEyes 是常驻的“电脑眼睛”：接入屏幕/窗口/摄像头/视频流/文件/系统音频等信号源，用**本地**规则与小视觉模型持续观察，只在异常或值得注意时记录事件、生成摘要、发桌面通知；默认不调用云端模型、不产生 API 费用。

本切片（v0.2）交付：

- 独立项目目录 + 文档（本文件、`PLAN.md`、`docs/identity.md`、`docs/memory.md`）
- 多种视觉信号源统一抽象：屏幕区域、窗口/进程画面、摄像头、本地视频、网络流（ffmpeg）、B 站直播、手机 ADB（接口）
- L0 反射层（运动/画面全黑/屏幕黑屏/冻结）→ L1 本地 VLM（qwen3.5-9b）关键帧判断 → L2/L3 预留
- 并行注意力 worker（高变化画面用多个中级模型轮转，压低排队延迟）
- 事件库 M0（JSONL）+ M1（SQLite）+ 关键帧；M2 digest；M3 recall
- 非人脸身份/在场基线（键鼠空闲 + 前台窗口）
- 文件监听信号源
- 声卡 loopback “耳朵”（响度事件）
- 桌面通知（warning+）
- 监督进程（崩溃自动拉起）、登录自启、幂等启停脚本
- `--selftest`（无摄像头/无 GPU 也可确定性验证 L0/L1/JSONL/文件/记忆/身份）

非目标（本切片不做，仅留接口）：人脸识别/生物认证、OSNet 身体 Re-ID 的具体模型、云端预算闸门实际调用、机械臂/云台、手机实时视频流（仅截图接口）。

## 2. 架构

```
信号源 readers ──► L0 规则层 ──► 事件队列 ──► attention workers (L1 本地 VLM)
 (camera/screen/     (帧差/亮度/     (有界队列)      (round-robin model_pool)
  window/video/      空闲/冻结)                          │
  network/phone)                                       ▼
      │                                       事件记录 M0/M1 + 关键帧
      │audio loopback ──► audio_loud 事件
      │identity ───────► identity_state 事件
      │filewatch ──────► file_changed 事件
                                                    │
                                                    ▼
                                     digest M2（本地 LLM，小时/日）
                                     recall M3（关键词+时间检索）
                                     notify（桌面气泡，warning+）
```

- 单进程、多线程（采集线程 + N 个 attention 线程 + 音频/身份/文件/心跳/digest/保留/状态线程）。
- L2（27B/35B）与 L3（云端）本切片不自动调用，仅保留事件字段与配置位置。

## 3. 事件模型（M0）

`F:\realeyes\events\events.jsonl`，一行一个 JSON 对象（UTF-8，追加写）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | str | `evt_<时间戳>_<6hex>` |
| `ts` | str | ISO8601 带本地时区 |
| `source` | str | camera / screen / audio / identity / file / heartbeat / check |
| `kind` | str | motion / camera_covered / screen_blackout / screen_freeze / audio_loud / identity_state / file_changed / heartbeat / check |
| `severity` | str | info / warning / critical（规则事件有下限，VLM 只能升级不能降级） |
| `summary` | str | 中文一句话（<=40 字） |
| `signals` | obj | 规则层的原始信号（luma、变化占比、idle_s 等） |
| `vlm` | obj | 本地 VLM 结果：summary/people/activity/risk/notable/severity/model/latency_ms/usage；规则事件为空 |
| `budget` | obj | cloud_used_today / cloud_cap（云端闸门预留） |
| `keyframes` | str[] | 关键帧路径（L1 调用前保存） |

关键帧：`F:\realeyes\events\YYYYMMDD\<event_id>\kf_00.jpg`。

## 4. 记忆分层

- **M0 原始层**：events.jsonl（事实来源，只追加）。
- **M1 索引层**：`state\realeyes.db`（SQLite：events 表 + digests 表），服务写入。
- **M2 摘要层**：`digests\hourly-*.md`、`digests\daily-*.md`，本地 9B 生成，纯 markdown。
- **M3 检索层**：`memory.recall()`：关键词 + 时间窗（默认 24h），按时间倒序；`realeyes.py --recall TEXT --hours N` 同时搜事件与 digest 文本。

## 5. 身份与在场（非人脸基线）

见 `docs/identity.md`。MVP 只使用：Windows `GetLastInputInfo` 空闲毫秒 + 前台窗口标题/进程名；输出 `presence ∈ {at_desk, recent, away}`、`confidence`、`foreground`。不做人脸；不做生物核验。

## 6. 配置

`src/config.yaml`（内联注释为文档）。关键段：`service`、`gateway`、`camera`（含 `source` 多源）、`screen`、`attention`（`workers`/`model_pool`）、`identity`、`filewatch`、`audio`、`heartbeat`、`digest`、`notify`、`storage`、`phone`、`cloud`。

`camera.source` 语法：

```
0 | 1                        本地摄像头（DirectShow 索引）
window:<标题子串>             窗口/进程画面捕捉（可配 window_crop）
screen:x,y,w,h               屏幕区域
bilibili:<room_id>           B 站直播（ffmpeg 拉流）
https://… / rtsp://…         网络流（ffmpeg rawvideo 管道）
D:\videos\demo.mp4           本地视频文件（循环播放）
phone                        手机 ADB 截图
```

## 7. 运行与验收

- 自测：`python src\realeyes.py --selftest`（8 项：motion+L1、camera_covered 保持 warning、JSONL、文件监听、记忆召回、身份快照、…）。
- 单帧检查：`--check`；短跑：`--duration N`；召回：`--recall TEXT --hours N`。
- 验收（本切片）：
  1. selftest 全 PASS；
  2. 用窗口/直播或视频源跑 `--duration` ≥60s，产生 motion 事件且带本地 VLM verdict 与关键帧；
  3. `identity_state` 与 `file_changed` 事件各至少 1 条（由 12s 间隔的测试配置触发）；
  4. `--recall` 能按关键词检索到上述事件；
  5. 长跑服务与监督进程逻辑不回归（见 PLAN.md 阶段 M0）。

## 8. 安全与隐私

- 默认全本地；云端 tier 关闭。
- 关键帧/事件只写本机 `F:\realeyes`；保留策略 `storage.keep_days` + 磁盘下限。
- 通知只发桌面气泡；短信/邮件为后续接口。
- 不在无人值守时执行有副作用的操作（通知优先）。
