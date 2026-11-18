# v0.9 跨天工作接续验证

以 v0.8 main `57fcb4c` 为基线，v0.9 运行代码与界面见 `4c8d61f`。本轮复用同一个虚构发布项目回答三个问题：隔天能否找回决定，连续纠正会不会覆盖其他事项，重启后究竟哪个审批可以写文件。[版本方案](v0.9-plan.md)

## 三个场景

| 场景 | 操作与实际结果 | 证据边界 |
| --- | --- | --- |
| 隔天找回决定 | 昨天 15:00–15:02 各有一句决定、分工和候选日期，今天新增 Linux 讨论。改述询问首轮给哪些用户开放、谁准备、排期是否敲定。简报三项均有引用，Windows 属于已确认的决定，顾言整理清单属于下一步，周五候选属于待确认。今天的 Linux 没进入简报。前一天的空窗口返回 0 条；即使附加 Markdown，空时段也不能生成。 | 虚构会话、真实 GPT-4.1，3 个目标事实、1 个窗口外干扰项。小范围全部入选；本例无法衡量大范围检索召回率。空日期、跨午夜及资料排除另有离线回归。 |
| 连续纠正 | v1 的清单负责人是顾言。文字提交“兼容清单负责人由顾言改为林岚。”生成 v2。提交“周五不是截止日期，发布日期待确认。”生成 v3。v3 保留林岚、Windows 及原引用，新增两条独立纠正来源，日期仍列为待确认。首稿、v2、v3 有效引用数分别为 3、4、5。产品界面先编辑再提交，然后按任务定位生成的版本和父版本差异。 | 模型探针提交两段文字。语音设备入口、本地 ASR 识别该否定句、界面上编辑模拟转写各由不同检查覆盖。没有真人连续口述与点击的端到端样本。 |
| 审批保存与恢复 | 通过应用审批接口拒绝 v1 与 v2，均未创建文件；批准 v3，落盘 Markdown 等于该版本导出。另一次真实 SQLite / HTTP / LangGraph 检查跨两次启动，恢复原会话与文档但未开监听、模型或审批。异会话恢复被拒绝；返回原会话重新提请后旧按钮仍失效，只有新按钮能保存。继续修订后删除原文，旧写入再次被阻止。 | 真实文件和数据库；重启测试的模型使用确定性替身。程序点击验证应用行为，人工决定的质量待观察。 |

### 实模调用

Windows 本机，OpenRouter `openai/gpt-4.1`，中文，温度 0，配置上下文 32768。用时从创建该任务到 API 可见待审批，包含本机调度及首次加载。三阶段各一次请求，服务回报的费用均可得。这里只核对功能，没有基线比较、重复运行或时延分位数。

| 阶段 | 用时 | 输入 / 输出 token | 服务回报费用（美元） |
| --- | ---: | ---: | ---: |
| 简报 v1 | 11.515 秒 | 345 / 30 | 0.000930 |
| 负责人 v2 | 1.297 秒 | 397 / 25 | 0.000994 |
| 否定日期 v3 | 1.234 秒 | 398 / 42 | 0.001132 |
| 合计 | — | 1140 / 97 | 0.003056 |

运行目录 `.runtime/continuation-probe/5d93b6235ac84180b5fe227b067f50c3/` 保存本次合成文档和原始计量；Git 中提供可重建的样本及探针。以上调用发生在 `e019785` 的生成路径，之后对显式更新来源和快照登记的修复使用离线回归。Ollama 沿用既有模型入口，本次未复测其简报质量。[模型探针](../scripts/probe_continuation.py)

### 声音与文字的边界

本机枚举 3 个 Windows WASAPI 输入。产品录音入口收到默认麦克风首帧后立即取消并释放；没有存储、转写或上传环境声音。Windows SAPI 合成 4.51 秒的固定句“周五不是截止日期，发布日期待确认。”，由本地缓存 Faster-Whisper base、CPU/int8 经录音纠正路径实际返回“週五不是截止日期 發布日期待確認”。本例保留否定词与待确认状态，有繁简和标点差异。该结果没有覆盖真人姓名、自然语速、串音或云 ASR。首次沙箱内语音合成失败，单独重试合成步骤成功。[语音探针](../scripts/probe_correction_voice.py)

## Windows 构建与交付

运行代码的基准提交为 `4c8d61f`，随后仅补验证文档。提交 `38331ad` 的 [PR 回归](https://github.com/MuzeAnisichael/GreatSage/actions/runs/44245574893) 为 **489 passed，3 skipped，2 warnings**（pytest 100.32 秒），冻结前端的语法检查、后端 smoke 与仓库检查均通过。[分支回归](https://github.com/MuzeAnisichael/GreatSage/actions/runs/44245540482)

- PyInstaller 6.22.2 冻结后端通过 **14/14**，合成服务 4 次模型调用和 1 次工具执行。原始记录位于 `.runtime/backend-smoke/20261118T053821Z-2e939b/result.json`。
- Electron 40.10.6 的实际应用目录通过 **78/78**，包含接续工作台、可编辑模拟转写与审批。packaged=true、parallelSources=true、live=false，退出码 0，stderr 为空。原始记录位于 `.runtime/desktop-smoke/2026-11-18T05-39-43-900Z/result.json`。
- 首次 Electron 压缩遇到缓存重命名权限错误。复用已验收的同一应用目录，electron-builder 26.15.3 通过 `--prepackaged` 生成 ZIP 与 NSIS。ZIP 的 19 个 UI 文件和源码哈希一致，其中桌面可执行文件、冻结后端与 app.asar 均匹配已验收目录。构建日志位于 `.runtime/v09-pyinstaller.log` 与 `.runtime/v09-release-build.log`。

| [Windows 预发布资产](https://github.com/MuzeAnisichael/GreatSage/releases/tag/v0.9.0-alpha.1) | 字节数 | SHA256 |
| --- | ---: | --- |
| GreatSage-0.9.0-alpha.1-win-x64.zip | 292306926 | `c76c282af7683127a4dce94f23093c1a99ec331e4b64131f7374b5dae379f20c` |
| GreatSage-0.9.0-alpha.1-win-x64-Setup.exe | 218596203 | `5fcf96e82b2f9d1a404dfab391f0caee2ffc6f7df65f85a4ea94dab0923e5466` |

同页提供 `SHA256SUMS.txt`。实验性安装器 ProductVersion 为 0.9.0-alpha.1，Authenticode 状态为 NotSigned。安装、升级、卸载与干净设备实测仍待完成。

## 定向回归与复现

- 引擎与选源 9 项定向检查及 1 项显式刷新回归通过。默认更正逐条替换，显式重选时按版本顺序重放可读取的用户纠正。新资料缺少被纠正条目或超预算时停止并保留原版。[引擎与来源测试](../tests/test_continuation.py)
- 语音模块 11 项定向检查通过，覆盖设备独占、草稿返回、超时与会话／版本变化。UI 模块纳入 `npm run check`。[语音测试](../tests/test_correction_voice.py)
- 恢复验收发现旧点击 ID 会随审批卡重新启用。修复保留旧卡作为失效历史，签发新卡。一次开发中接口签名过渡造成的 TypeError 已消除，最终跨两次启动的完整 HTTP 用例通过。[恢复验收](../tests/test_continuation_api.py)

可从旧会话的“接续工作”复现完整交互。用虚构资料检查“昨天”和空日，改一次负责人，再编辑一条含“不是”的录音转写。审阅版本差异，先拒绝一次保存，重启后手动提请并批准新版本。开发环境可运行以下命令。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_continuation_api.py -q --basetemp .runtime/pytest-v09-reproduce
.\.venv\Scripts\python.exe scripts/probe_continuation.py
.\.venv\Scripts\python.exe scripts/probe_continuation.py --cloud
```

第二条仅显示离线计划。第三条使用已配置的 OpenRouter 凭据和虚构测试资料，会产生模型费用。公开仓库不包含本地数据、音频或凭据。日常准确率、人工安装升级及长时稳定性仍需独立样本。
