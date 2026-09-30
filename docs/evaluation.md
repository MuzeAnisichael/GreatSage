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
# v0.3 纪要／待办／文档任务：默认使用离线夹具模型，不访问网络
.\.venv\Scripts\python.exe scripts/evaluate_tasks.py --output .runtime/eval/tasks-offline.json
# 同一任务集调用配置的语言模型；拒绝所有审批请求，不执行有副作用的工具
.\.venv\Scripts\python.exe scripts/evaluate_tasks.py --live --repeats 3 --chat-repeats 5 --output .runtime/eval/tasks-live.json
```

`evaluate_tasks.py` 使用 `evals/tasks.json` 中的虚构会议和 Markdown 资料，每次在独立数据目录运行，关闭语义检索和后台压缩。各指标的口径：

- **任务总耗时**：从创建任务到收到 `task_finished` 事件。**首个进度**：到任务进入运行状态的事件。
- **模型调用时延**：各步骤记录的首字和完成时间，重试次数单独统计。
- **待办**：按关键词匹配期望的待办，负责人和期限规范化后按包含关系比较。决定和未决问题检查关键词是否同时出现在纪要中。
- **引用有效率**：所有产物中有效引用的占比。
- **注入**：任何待办的负责人都不含资料里要求替换的名字时，才算未被执行。
- **对话时延**：带工具定义和不带工具定义的请求交替发送，以分散网络波动。
- **工具提议**：请求保存文件后等待审批出现，然后拒绝，并检查文件没有写入。
- **本地耗时**：40 个合成 Markdown 文件的导入和检索，以及一次可撤销写入。

离线夹具模型的抽取规则很简单，它的待办分数只用来检查管线，不代表模型质量。

各报告包含版本、配置、语料哈希及指标。添加 `--compare <旧报告路径>` 可比较同一 suite/语料的数值，语料不匹配会拒绝；配置变化会提示。`evaluate.py` 的报告按 suite 分组，其他脚本使用单套报告，二者不能交叉比较。改变重复次数或模型会影响解释；P50/P95 的样本数总是一起报告。

桌面回归：`node_modules/electron/dist/electron.exe scripts/smoke_desktop.cjs` 使用隔离数据、默认不请求云端；`--packaged` 加载已构建目录包的 app.asar 与冻结后端，`--live` 增加实际服务与静音播放测试，`--parallel-sources` 增加并发设备枚举。测试不打开麦克风；截图、审计下载副本和结果位于 `.runtime/desktop-smoke/`。Windows 沙箱如拒绝进程通信，应在正常桌面权限下执行，不关闭 Electron 的进程沙箱。

中文识别以 CER 为主，只统一 Unicode、大小写、空白和标点，不替换数字、同音字或简繁体。中文 WER 未做分词，不作为主要质量结论。单独 ASR 的延迟不包含 VAD；pipeline 从最后一帧 VAD 语音计算到首字/音频就绪，仍不等于客户端真正播放。长录音有多个分段时，CER 使用所有最终转写拼接，时延统计最后一个完整轮次。

回应集是开发场景，不能据满分推断自然会话准确率；记忆集规模小。长期摘要测试检查虚构项目/口令字面保留率、层级和来源失效，不证明复杂事实之间的语义关系保持正确。云端模型存在波动，因此同时记录失败、取消和远端返回的用量；取消后未收到用量标为未知，不计成零成本。

CPU 秒与峰值 RSS 仅覆盖 Python 测量进程（250 ms 抽样），不包含 Electron、Ollama 和驱动；不代表整机峰值或一天持续监听的资源量。公共录音来源、许可、格式转换及校验值见 `evals/audio/ATTRIBUTION.md` 和 `evals/speech.json`。
