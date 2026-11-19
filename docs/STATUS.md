# RealEyes — Status

> 🚧 施工中。最后更新：2026-11-19。事实以本机文件与运行数据为准。

## 已完成

| 模块 | 状态 | 说明 |
|---|---|---|
| 项目结构 | ✅ | `src/ docs/ scripts/ integrations/` |
| 多源采集 | ✅ | screen/window/camera/local video/network stream/phone（`_open_camera`） |
| L0 规则 | ✅ | 运动、全黑/遮挡、屏幕黑屏、冻结；`--selftest` 覆盖 |
| L1 本地 VLM | ✅ | 关键帧 → 本地 Qwen VLM → 结构化 JSON 判断 |
| 并行注意力 | ✅ | `attention.workers` + `model_pool`；网关 `--parallel` 支持 |
| 事件库 | ✅ | `events.jsonl` + SQLite + 关键帧 |
| 记忆 M0–M3 | ✅ | digest 生成；`--recall` 关键词/时间窗检索 |
| 身份基线 | ✅ | 键鼠空闲 + 前台应用（非人脸）；OSNet Re-ID 预留 |
| 文件监听 | ✅ | 目录新增/修改事件 |
| 音频 "耳朵" | ✅ | soundcard loopback 响度事件 |
| 通知 | ✅ | warning+ 桌面气泡 |
| 监督与自启 | ✅ | 崩溃自动拉起；登录自启由 HKCU Run 指向监督脚本 |
| 自测 | ✅ | `--selftest` 8/8 |
| 文档 | ✅ | 本目录 SPEC/PLAN/identity/memory/STATUS |
| 手机 ADB | ⏳ | 接口已实现；需手机授权 USB 调试 |
| 真实摄像头 | ⏳ | 运动链路需实际画面（开发机镜头当前不可用） |
| 部署切换 | ⏳ | 见 PLAN.md M1 |
| L2/L3 自动升级 | ⏳ | 事件字段与预算配置已留 |

## 运行要求

- Windows 10/11 + Python 3.10+
- ffmpeg 在 PATH
- 本地 OpenAI 兼容视觉模型服务（详见根 README 的"依赖"节）
- 可选：DeepSeek Harness 用于 `local_eyes` 集成

## 已知问题

1. 无摄像头画面时，运动规则不会触发；可用窗口/视频/网络流源做链路测试。
2. 手机 ADB 需要设备端确认 RSA 指纹；未授权时 `phone.py` 会返回明确错误。
3. 直播/视频平台源受平台条款与网络时效限制，仅作功能演示与自测。
4. 大模型与 ComfyUI 等其它 GPU 应用同时使用会争显存；网关会返回 503，服务自动跳过 VLM 调用。
5. 身份基线为启发式（空闲阈值），不是生物识别。

## 下一阶段

见 `PLAN.md`：M1 部署切换与 24h 稳定性 → M2 OSNet/记忆增强 → M3 手机/硬件/云端预算闸门。
