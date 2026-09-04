# v0.1 alpha 合成语音性能基线

记录日期：2026-09-04。三种配置均完成了识别、流式回答及系统语音生成；这几次运行的首段文字均未达到已确认的 1–2 秒目标。语音实际起播 2–3 秒的目标尚未验收，音频 ready 指标不能替代它。以下是本机固定合成样本的单次测量，不是稳定性分位数、真人识别准确率或性能保证。

## 测量方法

`scripts/benchmark_pipeline.py` 使用 Windows 系统 TTS 将“请用一句话回答一加一等于几？”写成音频文件，**不播放扬声器**。同一段 3.62 秒、16 kHz、单声道 PCM16 音频按 30 ms 分包，以单调时钟定时注入真实 Runtime；尾部补静音。实际 ASR、LLM、TTS 仍通过真实 Providers 调用。

- 音频内容 SHA-256：`b33b3e7e13795fa770a8b40c6518175776ca868ad1173b78cf4d697257dace55`。三行使用相同的 PCM 内容。
- 静音设置为 550 ms；Segmenter 按 30 ms 帧向上取整，实际阈值为 570 ms。快照间隔为 2.5 秒。
- LLM 为 OpenRouter `google/gemini-2.5-flash-lite`，上下文设置 8192、最大输出 128 tokens；要求简短中文回答。
- 回答语音使用本地系统 TTS。文字和语音语言均为 `zh-CN`，没有附加翻译调用。
- 计时原点是 VAD 判为语音的最后一帧，本样本位于输入开始后 2.85 秒，**不是 3.62 秒文件末尾**。
- 首段文字是进程内订阅者收到首个非空 `response_delta`；音频 ready 是收到首个 `audio` 事件，已完成语音合成和播放参考解码。
- 测量不含真实麦克风采集、WebSocket 传输、前端音频解码、扬声器起播或人耳听感；不能把音频 ready 称为实际起播时间。
- 设置与凭据从指定运行目录只读获取，每次历史、日志和样本保存在独立 benchmark 子目录，不使用用户对话。

## 完整管线结果

以下单位均为毫秒，各列来自同一行的一次运行。“最终 ASR”包括该次提供商调用的网络与处理耗时；“LLM 首文”从模型请求开始计算。后两列从上述 VAD 语音结束点计算。

| ASR 配置 | 端点检测 | 最终 ASR | LLM 首文 | 语音结束→首文 | 语音结束→音频 ready |
| --- | ---: | ---: | ---: | ---: | ---: |
| OpenRouter `openai/whisper-1` | 578.4 | 3122.6 | 806 | 4514.2 | 4606.5 |
| 本地 faster-whisper `base`，CPU/int8 | 576.6 | 375.2 | 1603 | 2560.5 | 2936.1 |
| OpenRouter `openai/whisper-large-v3-turbo` | 571.8 | 3653.0 | 822 | 5054.6 | 5988.3 |

| ASR 配置 | 最终 ASR 排队 | ASR 调用／取消数 | 首段 TTS 合成 | 监听准备 |
| --- | ---: | ---: | ---: | ---: |
| `whisper-1` | 0.64 | 2／1 | 62 | 2.3 |
| 本地 `base` | 0.49 | 2／0 | 55 | 887.6 |
| `whisper-large-v3-turbo` | 0.76 | 2／1 | 54 | 1.8 |

监听准备发生在注入音频前，本地模型加载时间没有混入语音结束后的指标。云端快照未完成时被最终请求取消；取消本地等待不能证明远端请求停止处理或不计费。本地快照在最终分段前已完成，因此没有取消。

本地 ASR 在本轮明显更快，但云模型首文等待更长。Turbo 在单独提供商探测中较快，**本次完整管线未复现该优势**；网络、服务负载和快照请求行为都需要进一步对照，不能仅凭其中最好的一次结果选定稳定延迟结论。首文到音频 ready 还包含后续文字生成及句子缓冲；本轮系统 TTS 本身仅约 54–62 ms，不能把整个差值归因于 TTS。

这些结果保留了未达到目标的情况。下一步应以同一套真人语料、多轮重复、同时记录服务用量和冷／热状态评估 P50/P95，再决定原生流式 ASR、快照策略、模型与句子缓冲的优化优先级。

## 固定样本的识别差异

输入：`请用一句话回答一加一等于几？`

| ASR 配置 | 实际最终转写 | 差异说明 |
| --- | --- | --- |
| `whisper-1` | `请用一句话回答 1 加 1 等于 g` | 数字与空格变化；“几”被替换成 `g`，是真实识别错误 |
| 本地 `base` | `請用一句話回答1加1等於機` | 繁简及数字格式变化；“几”被替换成“機”，是真实同音替换 |
| `whisper-large-v3-turbo` | `请用一句话回答1加1等于机。` | 数字、标点变化；“几”被替换成“机”，仍有同音替换 |

三轮模型均回答了 `1+1=2`，这说明该简单问题仍能被理解，**不代表转写完全正确**。报告中的 `punctuation_insensitive_match` 只忽略标点和空格，不自动转换繁简、汉字数字或同音字；应结合以上原文解释布尔结果。单个合成算术问题不足以衡量真人口音、停顿、噪声、重叠说话、专业名词和否定表达。

## 运行与复现

先验证配置，不调用任何模型或系统语音：

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_pipeline.py --tts-provider system --dry-run
```

执行已配置云端 LLM、指定 Turbo ASR、系统 TTS 的固定样本：

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_pipeline.py --asr-model openai/whisper-large-v3-turbo --tts-provider system
```

本地 `base` 模型已在设置页面准备后，可运行对照：

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_pipeline.py --asr-provider faster_whisper --asr-model base --tts-provider system
```

默认设置来源为 `.runtime`，输出根目录为 `.runtime/benchmark`。可用 `--settings-dir`、`--data-dir` 显式覆盖；脚本拒绝把用户运行目录作为 benchmark 根目录。每次运行创建新的子目录。当前结果在 `.runtime/benchmark/results.json`，各轮结果和合成 `fixture.wav` 同时保存在对应子目录。配置中的环境变量或已保存凭据只在调用时使用，报告不包含密钥。

其他可选参数包括 `--packet-ms 20`、`--partial-interval`、各组件的 `--*-model` 和 `--*-base-url`。切换到 Ollama 时须同时提供 `--llm-provider ollama` 与 `--llm-model` 指定本机已安装的模型。脚本不会下载本地 ASR 模型；默认复用设置的缓存目录或设置来源目录下的 `models/`。

表中原始运行标识：

- `whisper-1`：`run-20260904T113020Z-96ae6f3c`
- 本地 `base`：`run-20260904T112631Z-9feafbc8`
- Turbo：`run-20260904T113405Z-11e0c85b`

更早的云端诊断运行 `run-20260904T112256Z-93bf1bf0` 使用旧注入定时器，Windows 提前唤醒使端点测量比名义值低约 4 ms；该问题已通过 `perf_counter` 截止时刻复核修正。该早期运行不用于上述正式表格。

回归测试位于 `tests/test_benchmark_pipeline.py`，使用 mock 提供商验证实际 Runtime 的阶段先后、20/30 ms 注入、快照取消、历史隔离、失败与报告脱敏；它们不联网，也不替代上述真实服务测量。
