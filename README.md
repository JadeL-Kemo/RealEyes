# RealEyes

> 🚧 **施工中 (Work in Progress)** — 接口、配置与目录仍可能变化；欢迎 issue，但暂不建议直接用于生产或安防用途。
>
> [English](README.en.md) | 简体中文

RealEyes 是一套跑在本机的 **24 小时电子眼**：持续观察屏幕、窗口、摄像头、视频流、文件与系统声音，用**本地视觉模型**判断异常，只在需要时记录事件、生成摘要、发桌面通知——默认不消耗任何云端 API token。

## 特性

- **零 token 默认**：L0 规则 + 本地 Qwen VLM（llama.cpp / OpenAI 兼容网关），云端仅作为可选预算闸门。
- **多源采集**：屏幕、窗口/进程画面、摄像头、本地视频、网络流、手机 ADB（接口预留）。
- **四层逐级升级**：L0 规则 → L1 小 VLM → L2 本地大模型 → L3 云端（默认关闭）。
- **并行注意力**：多个 worker + 多模型轮询，适应游戏/直播这类高频变化画面。
- **事件与记忆**：JSONL + SQLite + 关键帧、小时/日 digest、`--recall` 关键词检索。
- **非人脸身份基线**：键鼠空闲 + 前台应用推断在场状态；OSNet 身体 Re-ID 预留。
- **其他信号**：文件监听、音频响度（"耳朵"），以及 L0 自检与崩溃自动拉起。

## 架构

| 层 | 做什么 | 运行位置 | 典型延迟 |
|---|---|---|---|
| L0 | 帧差运动、全黑/遮挡、屏幕冻结等规则 | CPU（OpenCV，无模型） | 毫秒级 |
| L1 | 关键帧交给本地小 VLM，输出结构化判断 | 本地网关 + qwen3.5-9b | 约 1–3 s |
| L2 | 疑难/高风险事件交给更大的本地模型 | 本地网关 + qwen3.8-27b / 35B-A3B | 数秒 |
| L3 | 云端模型，受每日预算与警告线约束 | 任 OpenAI 兼容服务 | 取决于网络 |

所有层都只在本机发生；L3 默认关闭，且需要显式配置 API key 才会启用。

## 快速开始

前置：Windows 10/11、Python 3.10+、ffmpeg、一个本地 OpenAI 兼容的视觉模型服务（见下方"依赖"）。

```powershell
cd src
python realeyes.py --selftest      # 无需摄像头/GPU：8 项自检
python realeyes.py --check         # 抓一帧并请求本地 VLM
python realeyes.py --duration 60   # 前台运行 60 秒
python realeyes.py --recall 关键词 --hours 24
powershell -File start_realeyes.ps1     # 隐藏后台（含网关检查）
powershell -File supervise_realeyes.ps1 # 崩溃自动拉起（登录自启可指向它）
```

配置见 `src/config.yaml`（`service.data_root` 默认 `F:\realeyes`，请改成你自己的数据目录）。数据包含事件库、关键帧、digest、日志与状态文件，均为运行期产物，不入版本库。

## 依赖（强依赖第三方组件，请先读）

RealEyes 本身**不包含**模型或推理框架，默认架构强依赖本机推理服务：

1. **本地 LLM 网关（必须）**：任意 OpenAI 兼容 `/v1/chat/completions` 服务，推荐用 [llama.cpp](https://github.com/ggml-org/llama.cpp)（MIT）加载 Qwen GGUF。
   - 推荐模型：**Qwen3.5-9B**（视觉，L1 默认）、**Qwen3.8-27B**（视觉，L2 可选）、**Qwen3.5-35B-A3B**（纯文本，L2 可选）。
   - 模型权重来源：unsloth 的 GGUF 仓库（Hugging Face / ModelScope），许可以模型发布页为准；权重不入库。
   - 参考实现与配置见 `integrations/local-llm-gateway/`；如使用带 API key 的网关，设置环境变量 `QWEN_LOCAL_API_KEY`（内置网关不校验，仅作占位）。
2. **ffmpeg（必须）**：窗口/网络流/直播源的解码，许可证为 LGPL/GPL（取决于构建）。需在 PATH 中。
3. **Python 依赖**：`opencv-python`（Apache-2.0）、`numpy`（BSD）、`mss`（MIT）、`soundcard`（BSD）、`PyYAML`（MIT）；可选 `onnxruntime`（MIT，用于后续 YOLO/OSNet）。
4. **Windows 专属能力**：DirectShow 摄像头、ctypes 调用 Win32（窗口标题/位置、键鼠空闲）；手机源需要 `adb`（Android SDK，Apache-2.0）并在手机上授权 USB 调试。
5. **可选集成**：DeepSeek Harness（DSH）插件 `local_eyes`（屏幕/图片 → 本地 VLM → 纯文字），见 `integrations/dsh-local-eyes/`，其抓屏与编码 API 来自 [dsh-see-now](https://github.com/SAOS-3624/dsh-see-now)（Apache-2.0）；DSH profile 接入片段见 `integrations/dsh/`。

模型权重、第三方工具、测试素材与对照数据均不随仓库分发。

## 信号源语法（`camera.source`）

```
0                        本地摄像头（DirectShow）
window:<标题子串>         窗口/进程画面（播放器、游戏、直播窗口）
screen:x,y,w,h           屏幕区域
bilibili:<room_id>       B 站直播流（ffmpeg 拉流，需要平台允许）
https://… / rtsp://…     网络流
D:\videos\demo.mp4       本地视频（循环，测试用）
phone                    手机 ADB 截图（需 adb 授权）
```

## 目录

```
src/           主服务 realeyes.py + identity/filewatch/memory/phone + 配置与启停监督脚本
docs/          SPEC / PLAN / identity / memory / STATUS
integrations/  本地 LLM 网关、local_eyes 插件、DSH 接入片段与全局指令示例
scripts/       run.ps1、selftest.ps1
```

## 文档

- [`docs/SPEC.md`](docs/SPEC.md) — 规格、事件模型、验收标准
- [`docs/PLAN.md`](docs/PLAN.md) — 里程碑与部署切换
- [`docs/identity.md`](docs/identity.md) — 非人脸身份设计
- [`docs/memory.md`](docs/memory.md) — 记忆分层与检索
- [`docs/STATUS.md`](docs/STATUS.md) — 当前进度、环境要求、已知问题

## 路线图（施工中）

- [ ] 真实摄像头运动链路与遮挡处理
- [ ] 手机 ADB 实时接入（截图流 / scrcpy）
- [ ] OSNet 身体 Re-ID、非人脸身份增强
- [ ] L2/L3 自动升级与云端预算闸门
- [ ] 部署脚本、服务化与配置向导

## 版权与许可

本仓库的代码与设计 © 2026 JadeL-Kemo，未经许可不得用于商业再分发。

仓库**不包含**任何第三方受版权保护的素材：模型权重、测试截图、测试数据、对照工具均不入库，仅以链接/说明形式引用。第三方依赖（llama.cpp、Qwen、ffmpeg、Python 包、DSH、dsh-see-now 等）版权归各自项目所有，请遵守其各自许可证。
