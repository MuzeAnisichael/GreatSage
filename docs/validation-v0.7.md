# v0.7 资料核对与审批保存验证

更新日期：2026-11-18。范围见 [v0.7 方案](v0.7-plan.md)。本记录区分源码机制、离线框架、真实服务和构建对象；未完成的检查不计通过。

## 已有定向证据

- `test_delegation.py`：真实 Deep Agents + 假 provider，首轮 12 passed / 1 failed；修正 StructuredTool 转换丢失 schema 限制后，仅复跑失败项通过。覆盖两个固定只读子 Agent、完整来源读取、逐字证据、核对覆盖、调用上限、取消、渐进 Skill 与父 checkpoint 不继承正文。
- `test_research.py`：首批 17 passed；审查后新增的恢复审计、资料重扫（等待审批/已完成）、线程排队身份四项定向 4 passed。真实 Runtime、SQLite、LangGraph、审批及文件工具，只有 delegation 是 fixture；不冒充模型质量验证。
- 既有 task/server/tools：34 passed / 1 skipped / 1 首次超时；研究框架改为按类型懒加载后，只复跑超时的纪要项通过。未重复全仓本地测试，最终 CI 另列。
- `npm run check`、相关 JS 语法、差异检查通过。新增桌面研究流程与关闭竞态场景，待最终包统一执行。
- 源码 `--check-research-runtime`：真实框架、8 次合成调用、两子 Agent 各 2 次，ok=true；约 10201 ms 含冷导入，不是模型响应时延。

## 真实服务与对比

`scripts/probe_research.py --cloud --model openai/gpt-4.1-mini` 使用 OpenRouter、32768 上下文、两个虚构 Markdown／三段来源，对比同输入的纪要与研究；程序通过应用审批入口保存，不等同于人工点击验收。测试未打开麦克风、使用私人会话或启用记忆提取。

| 轮次 | 纪要 | 研究 | 解释 |
| --- | --- | --- | --- |
| 首轮 `fd788f7` | 成功，1 请求，3778 ms，743 token，$0.0004652 | 请求前失败，0 请求，4691 ms | 内外层 LangGraph 上下文组合 AttributeError；独立诊断未覆盖的真实集成缺陷 |
| 修复后 `fae5404` | 成功，详见本地报告 | 7 请求后 schema 拒绝，20996 ms，8753 token，$0.0047348 | 两角色已读取来源，核对报告参数不合法；没有生成或保存产物，原始非法参数未留存，不能推测具体字段 |
| 格式修正后 `5a491ca`，同一 mini 模型 | 成功，1 请求，3938 ms，740 token，$0.0004604 | 引文不匹配拒绝，4 请求，13290 ms，4460 token，$0.0024272 | 证据错误没有触发格式修正，没有产物或保存；不计通过 |
| 同代码，显式改用 `openai/gpt-4.1` | 成功，1 请求，4894 ms，856 token，$0.003230 | 成功，9 请求，22261 ms，11965 token，$0.027758 | 文档3个引用均有效、无未知标记；一次 `succeeded/once/click` 保存，9/9用量覆盖 |

首轮报告：`.runtime/research-probe/20261117T175115Z-41606a/report.json`；第二轮：`.runtime/research-probe/20261117T175923Z-80384b/report.json`。这些是忽略的本机报告，输入与探针在仓库中可复现；第二轮已知服务用量7/7覆盖。耗时从创建任务到终态，含框架加载、队列与本地处理，不是纯模型时延。每轮仅一个任务样本，没有统计显著性、重复种子或性能提升结论。

第三轮报告：`.runtime/research-probe/20261117T180500Z-14345d/report.json`；最终完整模型报告：`.runtime/research-probe/20261117T180605Z-9a385c/report.json`。最终只增加探针对合成工具响应的本地留存，产品运行代码仍是 `5a491ca`；之后 `53df4b9` 仅将空待确认区域提示改得更准确，模型调用／校验未变。

逐条人工式材料复核由开发 Agent 阅读原文与两个导出文档完成，**不是用户人工验收**：两者均保留2032-06-14/2032-06-21两个冲突日期与林岚负责人，三条引用的资料版本／段落匹配。研究把“资料存在日期分歧”这个事实本身判为 supported，所以正文包含分歧而独立待确认区为空；已修改默认空区提示，避免暗示不存在分歧。纪要在这个样本也能指出冲突，不能宣称多 Agent 更准确。研究耗时／费用明显更多，普通任务保留短路径。

首次失败后真实外层图＋框架及取消回归共 2 passed。格式修正机制的框架边界8项通过，补本地 schema 引用处理后相关2项通过；真实 Runtime 的9调用修正／失败用量审计／审批保存集成1项通过。最终云端成功样本没有触发格式修正，不能把合成修正验证写成真实服务修正成功。

当前可报告 GPT-4.1 在本虚构样本上完成闭环；GPT-4.1 mini 仍有结构／引文失败，Ollama 本轮没有做实际研究模型验收。不会自动换模型、修改用户配置或下载权重。测试到此停止，没有为性能或成功率反复付费。

## CI、冻结与发布

早期代码 `fd788f7` 的 [Windows CI](https://github.com/MuzeAnisichael/GreatSage/actions/runs/44141140268) 为 **409 passed、3 skipped**，后端/JS/仓库检查通过；不代表随后修复的最终提交。该版冻结研究诊断为8合成调用、ok=true（3787 ms含冷导入），后端14/14，shutdown 422 ms／EOF 407 ms通过。报告分别在 `.runtime/backend-smoke/20261117T175335Z-51def5/result.json` 与 `.runtime/shutdown-smoke/842ffa1885cc8af9/`。

包内研究表单、角色／用量、逐次审批、关闭竞态检查通过；随后监控场景因测试脚本顶层变量重名失败，已改局部作用域。最终包需复跑，不将首轮报告记为整体验收成功。打包曾因受限缓存目录 rename EPERM 失败，后端已冻结；只重试必要构建阶段。

最终运行代码 **`53df4b9814edbe14530d3fc4c3966510b81175e1`** 的 [Windows CI](https://github.com/MuzeAnisichael/GreatSage/actions/runs/44144530655) 全部通过：**413 passed、3 skipped、2 warnings**（125.98秒），后端、JS和仓库检查通过。随后只更新交付文档，没有继续改运行代码。Python 3.11.7、Electron 40.10.6、Deep Agents 0.7.28、LangChain 1.4.6/core 1.6.11、LangGraph 1.2.15；固定依赖以 pyproject 为准。没有人工安装/升级或整天运行验收，仍保留 v0.5/v0.6 的未测项。

| 最终冻结对象 | 结果 |
| --- | --- |
| 研究依赖诊断 | ok=true，8次合成调用，两个角色各2次，4225 ms含冷导入；无网络或用户配置 |
| 记忆依赖诊断 | ok=true，真实 LangMem/trustcall，1次合成提案，853 ms |
| 语音依赖诊断 | ok=true，Pipecat/Deepgram、静音管线与清理，1874 ms；不等于设备或识别质量 |
| 冻结后端 | 14/14；合成服务4次模型/1次工具，重启重新授权、无重复副作用，报告 `.runtime/backend-smoke/20261117T181043Z-63159b/result.json` |
| 包内桌面 | **64/64**，packaged=true/live=false，报告 `.runtime/desktop-smoke/2026-11-17T18-11-02-048Z/result.json`；研究页截图已查看 |

桌面研究场景使用界面合成响应，验证表单、角色、计划、未知费用、一次性审批和关闭后迟到响应；真实框架／保存另由集成回归和上述真实云端探针覆盖，不能混为物理鼠标加真实模型的单一验收。正常关闭代码未再变更，复用本版早期 shutdown/EOF 记录；最终桌面检查也确认子进程退出。

构建日志 `.runtime/v07-pyinstaller-delivery.log`、`.runtime/v07-desktop-delivery.log`。Hugging Face 日志缓存访问受限后回退控制台，不影响完成；没有下载模型。未发布的中间构建不作为交付产物。

## 发布产物

保留小切片经 [PR #10](https://github.com/MuzeAnisichael/GreatSage/pull/10) 交付 main；[v0.7.0-alpha.1 预发布](https://github.com/MuzeAnisichael/GreatSage/releases/tag/v0.7.0-alpha.1) 提供完整 Windows x64 ZIP、实验性 NSIS 及 `SHA256SUMS.txt`。标签指向合入后的交付提交；构建代码见上，不将文档提交冒充重新构建。ZIP仅补充x64文件名，没有重新压缩。NSIS Authenticode 状态 **NotSigned**。

| 文件 | 字节 | SHA256 |
| --- | ---: | --- |
| `GreatSage-0.7.0-alpha.1-win-x64.zip` | 292213092 | `901587f3fefb90478946ce867ca16863e4f559270b5f7a7bad64a71c58f41153` |
| `GreatSage-0.7.0-alpha.1-win-x64-Setup.exe` | 218509688 | `4064801d3c2d71d1ba3d0142e6d3bb52e7a504e59806429e9ff4d336bd0e9cfe` |

## 解释边界

- 目前限定两只读职责、最多 8 段/共 64000 字符来源，单段最多 32000；实际请求仍需适配模型上下文，超预算拒绝而非静默截断。
- 单次委派最多 16 次模型请求、单角色 6 次、40 次工具调用、180 秒。外层累计限制为 24 次请求、160000 保守预留 token 和 600 秒已记录的主动运行时间；实际远端用量以服务返回值为准，取消/进程中断时可能未知。
- 所有选定来源都被两个子 Agent 读取不代表没有遗漏或事实判断错误；逐字引用证明出处，模型核对结论仍需人工审阅。
- research 阶段中断后整体重跑；已完成阶段复用。子 Agent 不具有独立持久化、递归委派或外部操作能力。
- 保存始终绑定产物版本与内容哈希并逐次点击。已启动的外部文件写入不能保证可撤回；取消后结果未知时保留工具账本，不自动重放。
