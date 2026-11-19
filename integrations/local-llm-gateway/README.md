# integrations/local-llm-gateway

RealEyes 依赖的本机推理栈快照（源码在 `F:\scripts\llm`，本目录为可版本管理的副本）：

| 文件 | 说明 |
|---|---|
| `local_llm_gateway.py` | 按需加载/空闲卸载的 OpenAI 兼容网关（127.0.0.1:8322），支持 `/admin/status`、`/admin/unload`、`/admin/reload`、`/admin/quit` |
| `local_llm_models.json` | 三档模型配置（9B / 35B-A3B / 27B，路径、ctx、显存 guard、`--parallel`） |
| `start_local_llm.ps1` | 幂等启动（供 HKCU Run 登录自启） |
| `smoke_test_gateway.py` | 网关冒烟：health/models/非流式/流式/tool call/vision |

安装与模型下载见项目根 `README.md` 与 `F:\README.md`；本副本不包含模型权重。

> 注意：`local_llm_models.json` 里的路径是本机约定（`F:\platforms\llamacpp\llama-app\llama.exe`、`F:\models\llms\gguf\...`），换机器时改这里即可。
