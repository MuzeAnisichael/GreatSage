# v0.4 框架迁移验证

更新日期：2026-10-20（北京时间）。本页保留迁移历史证据，并追加 `0.4.0-alpha.1` 的交付验收。原始基线为 `5095c04`；本机 Windows、Python 3.11.7 的虚拟环境包含全局包，干净环境另由 Windows CI 覆盖。范围见 [v0.4 方案](v0.4-plan.md)，逐片记录见[开发日志](development-log.md)。

## 2026-10-20 · 交付验收

| 检查对象 | 本次结果 | 适用范围 |
| --- | --- | --- |
| OpenRouter + Microsoft Learn MCP | 真实原生工具提议、审批、一次调用、引用回答通过 | `google/gemini-2.5-flash-lite`；发现 3 个工具；调用 `microsoft_docs_search`，全程 8.416 秒，USD 0.0005393 |
| Ollama 本地工具 | 真实原生工具提议、审批、虚构文件读取、随机码回答通过 | 本机 `qwen3:8b`；全程 12.282 秒，含首次加载 |
| 最新冻结后端 | **14/14 通过** | 使用发布包内后端，合成回环 HTTP、MCP、审批中断／重启、账本与三类产物 |
| 最新包内桌面组件 | **32/32 通过** | `smoke_desktop.cjs --packaged --parallel-sources`；读取新 `app.asar` 和冻结后端，覆盖 UI／监控／桌宠／退出清理 |
| 发布归档 | 完整 ZIP、SHA256、文件清单检查通过 | Windows x64，282,643,741 字节，包含桌面与后端，未包含运行目录；签名状态 `NotSigned` |

真实服务每个成功案例只请求模型两次、执行一个工具；审批由脚本调用应用的 `click` 入口，不是物理鼠标或语音确认验收。测试明确配置上下文 16384、最大输出 1024，仅目标工具可以进入逐次审批。公开摘要在 [evals/results-v0.4.json](../evals/results-v0.4.json)，复现入口为 `scripts/probe_agent_tools.py --cloud --mcp-learn --ollama`；该脚本会实际调用指定服务，不能作为每次开发必跑项。

Microsoft Learn 的入口、传输与无需认证条件依据[官方说明](https://learn.microsoft.com/en-us/training/support/mcp)和[开发参考](https://learn.microsoft.com/en-us/training/support/mcp-developer-reference)。一次成功只确认当日服务与固定 SDK 的互操作；定义变化、断连、未知结果和旧审批由既有隔离回归覆盖，没有向公共服务注入故障。

本次保留的失败与修正：

- 8192 上下文的首次发现成功，随后被保守预算拒绝，模型／工具请求为零；明确提高预算后成功。
- 已装 `qwen3:4b` 在 256／1024 输出预算下均未调用工具；其模板固定进入思考。核对已装 8b 模板支持关闭思考后，使用 8b 一次通过；没有下载或修改模型。
- 云端任务本身成功，初版探针却读取了已清理的中间步骤；改为检查任务关联的最终消息，离线复核原始证据及引用，未重复云端请求。
- PowerShell 脚本执行策略阻止 `build.ps1` 启动，改为直接调用构建器；后端成功后，Electron 下载缓存权限失败，仅重试桌面阶段并复用已安装的 Electron。未修改系统执行策略或重复后端构建。

构建使用 PyInstaller 6.22.2、Electron 40.10.6、electron-builder 26.15.3。运行时代码及版本对应 `1f3689e`；后续探针和交付文档没有修改包内源码。冻结报告为 `.runtime/backend-smoke/20261020T074751Z-15a3d0/result.json`；包内桌面报告为 `.runtime/desktop-smoke/2026-10-20T07-47-55-454Z/result.json`。后者使用隐藏的开发 Electron 装载发布包资源，不能代替干净机器上从 ZIP 启动、安装／升级／卸载或签名验收。

归档名 `GreatSage-0.4.0-alpha.1-win-x64.zip`，SHA256 为 `d92c309175ede550eaa483e4b2bfa297cadb02f4a547a6789104bd069f299ae6`。发布入口与校验清单见 [v0.4.0-alpha.1 Release](https://github.com/MuzeAnisichael/GreatSage/releases/tag/v0.4.0-alpha.1)。本轮复用已通过的 `e662e3e` CI 基线，最终交付要求 [PR #5](https://github.com/MuzeAnisichael/GreatSage/pull/5) 的最终提交 CI 通过后合入 main，发布页记录对应提交；本机未再重复全量回归。

## 2026-10-17 · 自动回归与界面（历史）

| 阶段 | 本机结果 | 主要新增覆盖 |
| --- | --- | --- |
| 原基线 | 209 passed、5 skipped | v0.3 已有行为 |
| LangChain 模型适配 | 213 passed、5 skipped | SDK 不隐式重试，凭据／账号隔离，流取消，不完整响应，禁止继承远端追踪 |
| LangGraph 任务图 | 216 passed、5 skipped | 旧任务兼容、重启后已完成工具不重放、checkpoint 不含正文／异常消息 |
| interrupt / resume 审批 | 219 passed、5 skipped | 每个工具单独等待，等待释放执行位置，排队取消撤销授权，来源删除使审批失效 |
| MCP HTTP | 227 passed、5 skipped | 官方 SDK 模拟协议、JSON Schema、定义变化、断连未知结果、迟到发现和点击授权 |
| 运行监控 | **228 passed、5 skipped**（23.22 秒） | 认证、消息／来源／预算元数据、正文隔离与删除失效 |
| 同批工具未知结果保护 | **229 passed、5 skipped**（33.31 秒） | 首个 MCP 结果未知后，同条模型回复中的后续只读工具也必须拒绝 |

最终全量命令：`.venv/Scripts/python.exe -m pytest -q --basetemp .runtime/pytest-unknown-batch-final --tb=short --maxfail=3`。使用独立工作区临时目录，绕开本机系统临时目录的写入限制；没有为环境问题修改产品行为。`npm.cmd run check` 通过。跳过项仍包括需要显式许可的原生语音／音源测试和本机不可创建的符号链接测试；不计为通过。新增同批工具用例在修复前确实失败，修复后通过，未把原来只覆盖下一轮模型的测试当作同批工具已受保护。

源码 Electron 的合成数据检查 **31/31 通过**，覆盖设置、MCP 配置不被通用保存覆盖、不自动连接、任务节点与 checkpoint、请求消息元数据、正文不显示、无横向溢出、桌宠和退出后端清理。报告在本地 `.runtime/desktop-smoke/2026-10-16T16-38-39-271Z/result.json`。目录名使用 UTC，因此可能与北京时间日期相差一天。

Windows CI 在 `a73d3f6` 上为 **230 passed、3 skipped**，前端与仓库检查通过（[运行记录](https://github.com/MuzeAnisichael/GreatSage/actions/runs/38934676625)）；该提交尚未包含后来加入 CI 的 `smoke_backend.py` 步骤，不能将此结果当作新步骤已在 CI 运行。

README 截图已用隔离合成数据重新生成，共 9 张，包含新的运行监控页；没有麦克风或真实模型调用。报告在 `.runtime/screenshots/2026-10-16T16-56-00-797Z/result.json`，截图和脚本一并提交。

## 冻结后端与真实回环 HTTP

PyInstaller **6.22.2** 成功构建后端。`scripts/smoke_backend.py` 分别以源码和冻结可执行文件运行，均 **14/14 通过**：

- 不打开音源，不自动发现 MCP；显式发现通过官方 SDK 连接合成 HTTP 服务。
- 模型请求工具后停在审批，checkpoint 已落盘，未点击前工具调用次数为零。
- 终止测试进程并重启，旧审批失效，工具目录需重新发现；继续任务后仍须重新点击。
- 批准后工具只执行一次，模型回复成功。
- 显式选择重启前会话，生成带来源的纪要、待办和文档三类产物。
- 请求目录和上下文元数据可查，正文没有出现在监控响应中。

两次分别调用合成模型 4 次、合成工具 1 次，未访问付费服务或用户文件。冻结报告为 `.runtime/backend-smoke/20261016T164741Z-a8bc18/result.json`，源码报告为 `.runtime/backend-smoke/20261016T164852Z-8562b5/result.json`。源码模式已加入 Windows CI。

这些冻结／源码黑盒报告对应同批工具拦截补丁之前的迁移切片，用于验证依赖、协议和恢复；补丁另由最终源码回归覆盖，没有宣称上述冻结产物包含后来的修改。

首轮冻结检查在纪要阶段失败，原因是脚本选择了重启后新建的空会话；修正测试的会话选择后通过。这不是产品恢复失败，也没有放宽会话隔离。回环服务是自有合成夹具，不能据此宣称兼容所有第三方 MCP 服务。

## 2026-10-17 · 真实模型连通与回答（历史）

使用已有授权凭据、独立测试目录和固定算术问题，每种配置只调用一次：

| 模型 | 首字／总耗时 | 结果 |
| --- | --- | --- |
| OpenRouter `google/gemini-2.5-flash-lite` | 1,416 / 1,481 ms | 正确回答；返回费用 USD 0.0000039 |
| Ollama `gemma3:4b` | 13,984 / 14,077 ms | 正确回答 |
| Ollama `qwen3:4b` | 27,900 / 28,535 ms | 连通，但 64 token 额度内仍是冗长分析，**回答质量未通过** |

冷／热启动未控制，样本仅一个问题，没有计算分位数或宣称性能提升。上述计时不包含 ASR、VAD 或 TTS，不能替代完整语音链路的首字与起播指标。当时尚未验收的真实工具／第三方 MCP 闭环，已在上面的 2026-10-20 记录补充。

## 保留的边界

- 本轮迁移模型、任务编排、审批、MCP HTTP 首片和本地监控；音频／ASR／TTS、记忆抽取与压缩仍是原有实现。
- MCP 尚无 stdio、OAuth、资源／提示词浏览、服务端采样或 elicitation；每次操作创建并关闭会话，外部工具只允许逐次点击授权。
- LangGraph checkpoint 只存控制状态；副作用去重和未知结果不重放由 GreatSage 账本负责，不能保证外部系统 exactly-once。
- 监控只展示本地状态与元数据；实体记忆、子 Agent、远端 tracing 尚未接入。上下文是估算，不是供应商精确计费值。
- 未完成真实会议／长会话、真实设备与回声、语音审批误识别、长时间运行、干净电脑安装／升级／卸载、原生签名和跨平台验收。
- 2026-10-20 的发布归档包含最新桌面和后端，包内组件及冻结后端通过检查；没有把这些结果等同于干净电脑的安装或长期使用验收。
