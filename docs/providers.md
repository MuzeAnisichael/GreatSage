# 模型与语音服务

GreatSage 将 LLM、语音识别（ASR）和语音合成（TTS）分别配置。每个阶段只调用所选服务，失败时记录错误，不自动把音频或文本转交给另一个服务。

## 已验证的配置

以下验证日期为 **2026-09-04**。模型目录会变化，安装时可运行 `scripts/probe_providers.py --catalog` 复核。

| 阶段 | Provider | 模型 | 其他设置 |
| --- | --- | --- | --- |
| 云端 LLM | `openrouter` | `google/gemini-2.5-flash-lite` | API 基址 `https://openrouter.ai/api/v1` |
| 云端 ASR，当前默认 | `openrouter` | `openai/whisper-large-v3-turbo` | `language: zh`；本轮中文短样本比 Whisper 1 更快，建议作为首版起点 |
| 云端 ASR，兼容选择 | `openrouter` | `openai/whisper-1` | 已实际调用，但本轮专名识别有错误 |
| 云端 TTS | `openrouter` | `qwen/qwen-audio-3.0-tts-flash` | `voice: loongjohn`；显式请求 MP3 |
| 本地 LLM | `ollama` | `gemma3:4b` | 基址 `http://127.0.0.1:11434`；使用已有模型，不自动下载 |
| 本地 ASR | `faster_whisper` | 例如 `base` 或 `small` | 默认 CPU / int8；先明确准备模型 |
| 本地 TTS | `system` | 无需模型名 | Windows SAPI；可按已安装声音选择中文或英文 |

API 密钥由设置模块从环境变量或用户配置的凭据中取得，仅在内存中交给 Provider。OpenRouter 通常使用环境变量 `OPENROUTER_API_KEY`。不要把密钥写到命令行参数、提交到仓库或放进日志。

OpenAI 兼容服务使用其自己的模型名称及 API 基址，例如 `https://api.openai.com/v1`。OpenRouter 的模型 ID 不能直接当作其他厂商的模型 ID 使用。

## 目录与端点

- 聊天：`POST /chat/completions`，以 SSE 增量接收文本与服务返回的用量。
- OpenRouter ASR：`POST /audio/transcriptions`，JSON 中携带 Base64 WAV 与 `language`；OpenAI 兼容 ASR 使用 multipart 文件上传。
- TTS：`POST /audio/speech`，接收音频字节，不直接播放。
- Ollama：`POST /api/chat`，读取 NDJSON；发送 `think: false`，不把独立的 `message.thinking` 字段当作回答。

查询专门的语音合成模型必须使用 `GET /models?output_modalities=speech`；`audio` 查询的是其他音频模型。ASR 查询使用 `output_modalities=transcription`。目录的 `supported_voices` 给出该模型声明的声音。

本次真实 `speech` 目录返回 18 个模型，其中不包含旧文档示例中的 `openai/tts-1` 或 `openai/gpt-4o-mini-tts-2025-12-15`，因此默认使用实际成功的 Qwen TTS。文档示例不能代替当前目录和实际调用验证。

参考：[OpenRouter TTS 文档](https://openrouter.ai/docs/guides/overview/multimodal/tts)、[转写文档](https://openrouter.ai/blog/tutorials/transcription-on-openrouter/)、[speech 目录](https://openrouter.ai/api/v1/models?output_modalities=speech)、[transcription 目录](https://openrouter.ai/api/v1/models?output_modalities=transcription)、[Ollama Chat API](https://docs.ollama.com/api/chat)。

## Windows 网络与本地服务

HTTPX 默认读取代理环境变量，但不会自动读取 Windows Internet Settings。GreatSage 在没有显式代理环境变量时使用 Windows 当前配置的 HTTP/HTTPS 代理；`localhost`、`127.0.0.1`、`::1` 的本地模型连接保持直连。TLS 证书验证始终开启。

实际排查中，沙箱外通过系统代理成功访问模型目录与云端语音服务。沙箱内受限网络的失败不等于密钥或模型配置错误。诊断只打印安全错误类别，例如 `connection_refused`、`tls_certificate_verification_failed`，以及 HTTP 状态；不会打印原始响应体、Authorization 或音频载荷。

Ollama 本机版本为 `0.33.2`。已有 `qwen3:4b` 在本次配置下虽然收到 `think: false`，仍把思考式文字放在正式 `content` 中；不能可靠地靠文本启发式删除。`gemma3:4b` 已得到正常的一句答案，因此用它作为本地示例。不同模型模板需要分别验证。

本地 ASR 需要 `faster-whisper` 依赖及相应模型文件。`load_local()` 和普通 `transcribe()` 只读取本地缓存，不会下载；只有明确调用 `warmup()` 才允许准备模型。模型缓存放在运行数据目录的 `models/` 下。取消或超时会停止等待结果；已经进入 CTranslate2 的计算可能继续到当前调用结束。

本地 TTS 需要 Windows SAPI 和 `pywin32`。本机实际枚举到 `Microsoft Huihui Desktop`（中文）和 `Microsoft Zira Desktop`（英文）。请求未安装语言或指定了不存在的声音会报错，不静默切换到云端。SAPI 输出临时 WAV，读取后清理，不通过扬声器播放。受限沙箱中 SAPI 曾返回访问被拒绝，正常桌面权限环境下生成成功。

MP3 回放参考解码需要基础依赖 `av`；它不应只随本地 ASR 可选依赖安装。系统生成的 PCM16 / 16 kHz / 单声道 WAV 可直接读取。裸 PCM 必须显式给出采样率，不能猜测后标成 WAV。

## 本轮实际数据及边界

这些是单次、短句、合成语音测试，**不是真人噪声环境准确率，也不是 P50/P95 或端到端时延保证**。测试期间网络、模型启动状态及机器负载会影响结果。

固定中文文本为：“大贤者已经准备好了。今天是语音功能测试。”

| 实验 | 输入 | 本次观察 |
| --- | --- | --- |
| Qwen TTS / loongjohn | 20 字固定中文 | 5002 ms；95611 字节 MP3；解码后 4.776 秒 |
| Whisper 1 转写上述云 TTS | 4.776 秒合成中文 | 3904 ms；正文一致，标点/空白不同；API 返回费用 USD 0.0005 |
| 中文 SAPI 独立生成 | 相同固定中文 | 333 ms；186286 字节 WAV；5.82 秒；没有播放 |
| Whisper 1 转写独立 SAPI | 5.82 秒合成中文 | 4740 ms；“大贤者”被识别成“大弦者”；API 返回费用 USD 0.0006 |
| Ollama gemma3:4b | “一加一等于几？只用一句话。” | 冷启动首字 22151 ms，总计 22241 ms；回答“一加一等于二。” |

随后使用同一条 5.82 秒 SAPI 音频做一次有界对照，每个模型各请求一次 2 秒快照、一次完整句：

| 模型 | 2 秒快照接口耗时 | 完整句接口耗时 | 完整句结果 | API 返回费用：快照 / 完整句（USD） |
| --- | ---: | ---: | --- | --- |
| `openai/whisper-1` | 3934 ms | 2748 ms | “大贤者”误写为“大弦者” | 0.0002 / 0.0006 |
| `openai/whisper-large-v3-turbo` | 1063 ms | 1183 ms | 本条文本与原句一致 | 0.00000666 / 0.0000193806 |

快照是对一段不完整音频的独立转写请求，不是 ASR 服务的流式最终结果。它可能改写专名、改变简繁体或补全未说完的内容，因此应在 UI 中作为临时文本展示，不能据此声称最终识别已确认。上述耗时不包括用户说话时间、VAD 等待、LLM 生成或回放。

TTS 二进制响应提供生成 ID，但没有本次请求费用字段；日志记录字符数与生成 ID，不编造费用。表中 ASR 费用来自该次服务返回的 `usage.cost`，不是长期价格承诺。

回放参考门控另有纯合成信号测试：19 项通过；200 个 20 ms 帧的一次处理均摊约 0.135 ms。它仅对高度相关且残差很小的信号静音，不能替代完整 AEC，也无法可靠区分噪声与极弱的用户双讲；耳机仍是首版验证的可靠配置。

原始测试结果保留在本地 `.runtime/` 中，不提交音频或凭据。Provider/probe 的相关单元测试使用模拟网络，覆盖流式解析、连接取消、有限超时、错误脱敏、失败后保留已完成结果以及正确的 `speech` 目录查询。

## 可重复执行的有界探测

以下命令会进行真实服务调用，可能产生小额费用，输出仅包含固定测试文本和安全诊断：

```powershell
.\.venv\Scripts\python.exe scripts/probe_providers.py --catalog --output .runtime/provider-catalog.json
.\.venv\Scripts\python.exe scripts/probe_providers.py --cloud --components tts,asr --tts-model qwen/qwen-audio-3.0-tts-flash --voice loongjohn --output .runtime/cloud-speech-probe.json
.\.venv\Scripts\python.exe scripts/probe_providers.py --cloud --components asr --compare-asr --output .runtime/cloud-asr-comparison.json
.\.venv\Scripts\python.exe scripts/probe_providers.py --ollama --local-model gemma3:4b --components llm --output .runtime/ollama-probe.json
```

`--compare-asr` 固定为两个已核对模型各两次转写，共四次短请求。每个阶段立即保存结果；某个服务失败不会丢弃之前成功的记录，也不会自动换服务重试。
