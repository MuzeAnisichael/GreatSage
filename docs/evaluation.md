# 可重复评测

所有命令在项目根目录运行。默认使用 `.runtime/` 设置；模型测试仅发送固定虚构文本或明确授权的 FLEURS 公共录音，不读取历史库。运行报告写入忽略目录。`--live` 会调用配置的服务并可能计费；原始录音、回答和用量可以在本机报告中查看。

```powershell
# 离线回应规则 / 跨会话关键词基线
.\.venv\Scripts\python.exe scripts/evaluate.py --output .runtime/eval/offline.json --compare evals/baseline-v0.1.json
# 实际旁听判断与嵌入，或 --suite memory / --suite decisions
.\.venv\Scripts\python.exe scripts/evaluate.py --live --output .runtime/eval/live.json
# 已安装 Ollama 模型；不会拉取权重
.\.venv\Scripts\python.exe scripts/evaluate.py --suite memory --live --embedding-provider ollama --embedding-model bge-m3:latest --output .runtime/eval/ollama.json
# 真人原始录音识别、纯数字模拟回声；去掉 --live 仅执行回声检查
.\.venv\Scripts\python.exe scripts/evaluate_speech.py --live --repeats 3 --output .runtime/eval/speech.json
# 降幅录音，仍不是实际房间或 VAD 测试
.\.venv\Scripts\python.exe scripts/evaluate_speech.py --live --variant quiet --output .runtime/eval/quiet.json
# 真实 VAD -> ASR -> LLM -> TTS；有节奏地注入公共录音，不打开麦克风或播放扬声器
.\.venv\Scripts\python.exe scripts/evaluate_pipeline.py --fixture-id fleurs-zh-2 --data-dir .runtime/pipeline --tts-provider openrouter --report .runtime/eval/pipeline.json
# 跨 32 个虚构会话的三层摘要、重启与删除验证；不加 --live 只输出计划
.\.venv\Scripts\python.exe scripts/evaluate_long_memory.py --live --output .runtime/eval/long-memory.json
```

各报告包含版本、配置、语料哈希及指标。添加 `--compare <旧报告路径>` 可比较同一 suite/语料的数值，语料不匹配会拒绝；配置变化会提示。`evaluate.py` 的报告按 suite 分组，其他脚本使用单套报告，二者不能交叉比较。改变重复次数或模型会影响解释；P50/P95 的样本数总是一起报告。

桌面回归：`node_modules/electron/dist/electron.exe scripts/smoke_desktop.cjs` 使用隔离数据、默认不请求云端；`--packaged` 加载已构建目录包的 app.asar 与冻结后端，`--live` 增加实际服务与静音播放测试，`--parallel-sources` 增加并发设备枚举。测试不打开麦克风；截图、审计下载副本和结果位于 `.runtime/desktop-smoke/`。Windows 沙箱如拒绝进程通信，应在正常桌面权限下执行，不关闭 Electron 的进程沙箱。

中文识别以 CER 为主，只统一 Unicode、大小写、空白和标点，不替换数字、同音字或简繁体。中文 WER 未做分词，不作为主要质量结论。单独 ASR 的延迟不包含 VAD；pipeline 从最后一帧 VAD 语音计算到首字/音频就绪，仍不等于客户端真正播放。长录音有多个分段时，CER 使用所有最终转写拼接，时延统计最后一个完整轮次。

回应集是开发场景，不能据满分推断自然会话准确率；记忆集规模小。长期摘要测试检查虚构项目/口令字面保留率、层级和来源失效，不证明复杂事实之间的语义关系保持正确。云端模型存在波动，因此同时记录失败、取消和远端返回的用量；取消后未收到用量标为未知，不计成零成本。

CPU 秒与峰值 RSS 仅覆盖 Python 测量进程（250 ms 抽样），不包含 Electron、Ollama 和驱动；不代表整机峰值或一天持续监听的资源量。公共录音来源、许可、格式转换及校验值见 `evals/audio/ATTRIBUTION.md` 和 `evals/speech.json`。
