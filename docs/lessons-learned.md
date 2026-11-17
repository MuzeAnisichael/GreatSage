# 开发经验与已知陷阱

更新日期：2026-11-18。这里只保留有复用价值的结论；过程见[开发日志](development-log.md)，设计见[架构](architecture.md)。改动相关模块前检索条目；新增证据补在原条目，失效经验注明替代结论。以下区分实测故障与代码审查发现的边界；具体执行与发布状态以相应版本验证记录为准，文档维护不代表重跑测试。

## L01 · 结果未知时，同批后续工具也要停止

- **症状／原因**：远端调用发出后断连，仅删除下一轮模型的工具定义，仍会执行当前模型回复中已经列出的后续工具。
- **处理**：在统一执行入口检查任务账本；有未知结果时拒绝尚未执行的同批调用，不新建审批、不自动重试。已完成调用读取账本。
- **何时复查**：调整工具批处理、并发、重试或多 Agent 委派时。checkpoint 不是外部副作用的 exactly-once 保证。
- **证据**：[2026-10-17 开发日志](development-log.md)、[迁移验证](validation-v0.4.md)；定位见[工具调用链](code-map.md)。

## L02 · 重启测试必须显式保留原会话

- **症状／原因**：首轮冻结后端黑盒在纪要阶段失败，夹具误选重启后新建的空会话。
- **处理**：脚本保存并明确传递原会话 ID；无需放宽产品会话隔离。
- **何时复查**：重启、会话续接、任务恢复、测试夹具初始化变化时。先区分数据选择错误与产品恢复故障。
- **证据**：[冻结后端记录](validation-v0.4.md)、[smoke_backend.py](../scripts/smoke_backend.py)。
- **v0.8 更新**：当前入口改为 `SessionsMixin.active_session()` 持久指针，重启不会再无条件新建空会话；历史故障仍说明夹具应明确来源 ID，而不能依赖默认选择。重启只恢复入口，不自动运行任务／后台模型；用户激活会话和显式恢复任务分开处理。见 [sessions.py](../greatsage/sessions.py)、[test_sessions.py](../tests/test_sessions.py)。

## L03 · 环境权限错误不要修成产品逻辑

- **症状／原因**：本机系统临时目录出现写入／替换限制；Electron 在受限执行环境中不能启动。
- **处理**：测试使用仓库内隔离且忽略的临时目录；图形进程按环境权限运行并读取落盘结果。执行权限仍遵守当前环境规则，不绕过限制。
- **何时复查**：同一测试换执行环境后批量失败或没有终端输出时。先做一次最小诊断，避免重复全量运行。
- **证据**：[开发日志中的迁移基线、运行监控及截图记录](development-log.md)。测试目录不能指向日常用户数据库。

## L04 · 框架迁移要显式接管默认行为

- **症状／约束**：框架 SDK 的环境凭据、追踪、自动重试和提供商字段不能直接当作应用约定。迁移中需要保留 OpenRouter 费用字段、补齐 Ollama 工具结果名称，并拒绝不完整响应流。
- **处理**：保留应用的凭据和连接池；关闭继承的远端 tracing 与 SDK 隐式重试；兼容补丁限制在薄适配层。
- **何时复查**：升级聊天 SDK、切换提供商或启用追踪时，核对实际版本，不重复做无关音频／安装测试。
- **证据**：[chat_models.py](../greatsage/chat_models.py)、[模型适配迁移日志](development-log.md)、[test_providers.py](../tests/test_providers.py)。

## L05 · 来源删除、取消和迟到结果必须共同处理

- **症状／原因**：任务持有来源正文或已批准但未执行的操作，单独删除数据库记录不足以阻止后续写回或执行。
- **处理**：来源删除先取消相关任务、撤销审批、清理上下文；异步提交核对来源／版本和代际。取消、重启后的未执行授权失效，恢复需重新确认。
- **何时复查**：新增后台写入、缓存、实体记忆、会话续接或子 Agent 时，把每份派生内容纳入来源失效路径。
- **证据**：[框架审批与取消边界日志](development-log.md)、[记忆与删除设计](memory-design.md)、[test_tasks.py](../tests/test_tasks.py)。
- **v0.8 补充**：会话切换也须在第一次清理 await 前撤销回复／音频／数据代际及任务 token，再把活动任务标为 interrupted、撤销审批并有界等待。只检查模型正文循环会漏掉 `_respond.finally` 的完成事件与工具任务创建；两处都要核对固定身份。见 [runtime.py](../greatsage/runtime.py)、[test_sessions.py](../tests/test_sessions.py)、[test_task_session_lifecycle.py](../tests/test_task_session_lifecycle.py)。

## L06 · 验证结果绑定对象，不能相互替代

- **症状／约束**：简单模型连通不代表回答／工具质量；音频 ready 不等于实际起播；源码通过不等于最新冻结包或安装器通过。
- **处理**：报告注明提交、环境、样本、计时点和构建对象。保留原始结果，新的源码修补不追溯覆盖旧构建证据。
- **何时复查**：发布状态、性能结论或模型兼容范围变化时。优先引用已有有效证据，只补当前缺口。
- **证据**：[性能基线](performance-baseline.md)、[迁移验证](validation-v0.4.md)；该记录中的冻结检查早于同批工具保护补丁。

## L07 · 验收最终回答要读持久消息，不依赖临时步骤

- **症状／原因**：真实工具任务成功后，探针从已按来源清理规则删除的中间步骤读取回答，误报空结果。
- **处理**：按 `task_id` 查找最终持久消息，再核对工具账本与引用；保留原报告，离线重新判定已有证据，不重发付费调用。
- **何时复查**：产物生成、任务收尾和来源清理策略变化时。测试夹具不能要求产品保留本应删除的正文副本。
- **证据**：[v0.4 公开摘要](../evals/results-v0.4.json)、[probe_agent_tools.py](../scripts/probe_agent_tools.py)。

## L08 · 构建权限问题只重试失败阶段

- **症状／原因**：本机 PowerShell 禁止执行脚本文件；Electron 下载缓存的重命名在受限环境中失败，后端已经构建成功。
- **处理**：不修改系统执行策略，直接运行脚本对应的 PyInstaller／electron-builder 命令；桌面阶段可通过构建器的 `electronDist` 复用已安装的相同版本 Electron。需要正常缓存访问时按执行环境申请权限，不重建已经完成的后端。
- **何时复查**：换构建环境或下载缓存失败时，先确认失败阶段与已有产物；不要把构建失败当作业务代码故障。
- **证据**：[开发日志](development-log.md)、[开发与发布说明](development.md)。

## L09 · 流式稳定片段不能直接成为用户命令

- **症状／原因**：云端 `is_final` 可以出现在一句话中间；强制分段也不代表说话结束。空 Finalize 确认被上游忽略时，已收到的稳定文字又可能一直不提交。
- **处理**：保留官方 SDK 的帧和时间区间，聚合稳定片段；本地结束后，等待覆盖人声末尾的远端端点，或完整覆盖本地结束边界的稳定区间。Finalize ACK 不保证返回，不能将每次强制结束都视为必有确认；短噪声丢弃不能升级为命令。时间区间不明、跨界导致归属歧义、积压或超时停止该流式源，不能猜测归属。正常的下一次开口改由 L13 的有界等待队列承接。
- **何时复查**：更换识别 SDK、轮次模型或恢复策略时，同时检查单连接、空确认、取消、来源代际和可选录音限额。
- **证据**：[voice_pipeline.py](../greatsage/voice_pipeline.py)、[streaming_asr.py](../greatsage/streaming_asr.py)、[test_voice_runtime.py](../tests/test_voice_runtime.py)。

## L10 · SDK 的导入、调试扩展与冻结都属于运行边界

- **症状／原因**：首批 PCM 中导入语音框架可能阻塞采集；Pipecat 1.13 会执行 `PIPECAT_SETUP_FILES` 的脚本；源码通过不能覆盖 SDK 的懒加载冻结依赖。
- **处理**：采集前预加载，停用未授权的环境脚本扩展；按实际进口路径补 PyInstaller hook 和许可元数据。包内运行离线诊断，明确不等于联网识别或设备验收。
- **何时复查**：语音框架升级、切换 Python 版本或调整构建排除项。Python 3.11 的上游 `wait_for2` 全局兼容补丁已记录，不能忽略取消语义变化。
- **证据**：[v0.5 验证](validation-v0.5.md)、[diagnostics.py](../greatsage/diagnostics.py)、[第三方记录](third-party.md)。

## L11 · 恢复识别必须隔离采集包和正在启动的旧管线

- **症状／原因**：只清空 Runtime 队列会漏掉采集分发线程尚未送达的包；用“当前来源身份”处理旧异步启动的异常，会误停新管线。
- **处理**：先提升来源代际，重试期间丢弃输入，建立包时间截止点；所有启动、提交与异常使用操作开始时的固定身份。每个 await 后核对所属会话、采集和配置。设备停止单独跟踪，不能把识别重连说成设备恢复。
- **何时复查**：引入自动恢复、音源热切换或多连接时。包时间不是声卡采样级精度，不应作更强声明。
- **证据**：[test_voice_recovery.py](../tests/test_voice_recovery.py)、[v0.5 验证](validation-v0.5.md)。
- **清理补充**：禁止旧回调不等于资源已释放。并发 `close()` 需等待同一清理结果，不能仅因 `closed` 标志提前成功；否则新连接会与旧资源清理重叠。验证见 [test_voice_pipeline.py](../tests/test_voice_pipeline.py)。

## L12 · 话轮版本和来源失效不是同一种边界

- **症状／原因**：上一句已得到双方端点确认，下一次 VAD start 却让异步 final 提交因版本变旧而丢失。
- **处理**：临时转写与 final 都保留所属话轮身份；已确认 final 保持来源／会话／数据代际检查及区间去重，不因下一句开始而失效。上一句尚未确认时由 L13 的队列和时间区间匹配处理，不能把新话轮开始本身当作歧义。
- **何时复查**：流式端点、事件广播或提交调度变化时，用受控 await 屏障复现顺序，不靠重复真实音频碰概率。
- **证据**：[test_streaming_commit.py](../tests/test_streaming_commit.py)，修前失败、修后通过。

## L13 · 连续话轮要分别处理采集、确认与回应

- **症状／原因**：本地已停顿但云端尚未确认时，立即停止音源会打断自然交流；只按“当前话轮”接收回调，又可能把上一句 final 写到下一句，或在用户继续讲话时触发旧回答。采集包时间与服务音频区间不是同一时钟。
- **处理**：流式输入按 30 ms 切分，以含静音的完整 PCM 时间轴匹配最早未确认轮次；每源保留最多四个较早的待确认话轮加当前轮次，并逐轮串行提交。时间戳仍非样本级精准，明确跨界、队列满或本地结束后 15 秒未确认时停源。全部已确认原文保留，只延后回应触发并保留最新一轮；迟到或延后的语音不得批准开口后才出现的工具操作。
- **界面与隔离**：用稳定 `turn_id` 更新原转写行，以 `speech_phase`／`pending_turns` 区分收音和确认；停止后的临时文字标明未提交，会话切换清空实时列表。会话、来源和数据代际检查继续独立执行。
- **何时复查**：调整服务端点、静音阈值、并发提交或语音审批时。优先用受控回调顺序验证，不靠重复付费语音碰概率。
- **依据与边界**：[voice_turns.py](../greatsage/voice_turns.py)、[话轮调用链](code-map.md#v05-音源话轮与取消链)、[test_voice_turns.py](../tests/test_voice_turns.py)、[test_streaming_commit.py](../tests/test_streaming_commit.py)、[桌面冒烟脚本](../scripts/smoke_desktop.cjs)。真实连续语音与 Deepgram 尚未验证；最新执行证据见 [validation-v0.5.md](validation-v0.5.md)。

## L14 · 提取为空与不支持协议必须区分

- **原因**：LangMem 可以把没有工具调用的普通回复返回为空列表；直接提交进度会悄悄跳过整批来源。框架 `max_steps=1` 也不等于 trustcall 内部没有重试。
- **处理**：使用明确 NoMemory schema，普通文本或无效提案失败时不推进检查点；限制 trustcall 尝试与实际请求次数。确定性错误暂停，瞬态最多三次自动尝试，安全暂停标记跨重启保留。手动操作／配置变化才解除。
- **何时复查**：升级 LangMem/trustcall 或更换模型时检查真实最终请求及工具输出，不能用 SDK 假模型成功代替实际模型能力。
- **证据**：[memory_extraction.py](../greatsage/memory_extraction.py)、[test_memory_extraction.py](../tests/test_memory_extraction.py)、[v0.6 验证](validation-v0.6.md)。

## L15 · 有界批次也会发生饥饿，取消也可能不合作

- **原因**：最早八条都超预算会让第九条永远取不到；对子任务直接 `await cancel` 会等待吞取消的服务。只测无设备编号的麦克风标签也会漏掉真实来源。
- **处理**：超长完整原文按预算指纹延后，既不截断也不标已处理；新预算重新尝试。前台取消最多等待 0.25 秒，保留旧任务占位并用代际拒绝写回，避免无限替代任务；来源谓词覆盖真实 `microphone:<device>` 标签。
- **何时复查**：加入新后台任务、输入来源或上下文预算时，优先用具体竞态及长来源夹具检查，不反复运行性能基准。
- **证据**：[test_memory_runtime.py](../tests/test_memory_runtime.py)、[test_structured_memory.py](../tests/test_structured_memory.py)。
- **v0.8 补充**：相同原则扩展到任务取消／会话挂起。使用 `asyncio.wait` 有界等待后仍保留未结束 job，直到它真正完成才移除；同一任务旧运行仍未退场时拒绝 resume，避免两个 runner 共享账本。ContextVar 固定每次运行的 token／会话／设置／数据身份，旧 runner 的 finally 也不能结束替代运行。见 [tasks.py](../greatsage/tasks.py)、[test_task_session_lifecycle.py](../tests/test_task_session_lifecycle.py)。

## L16 · 弹窗关闭要同步失效，后台刷新不能负责打开

- **症状／原因**：实体弹窗关闭后被异步列表刷新重新打开，拦住冲突入口；原生 `close` 事件排队执行，不能用它作为唯一的请求失效时刻。
- **处理**：用户关闭时先同步清理选择与详情代际；列表和详情分别维护请求身份。只有用户入口打开弹窗，后台只能更新仍打开且身份一致的详情；迟到的成功和失败都丢弃。
- **何时复查**：新增弹窗自动刷新、详情跳转或原生窗口事件时，用可控 Promise 在关闭与事件回调之间释放响应。
- **证据**：[structured-memory.js](../ui/structured-memory.js)、[包内界面回归](../scripts/smoke_desktop.cjs)、[v0.6 验证](validation-v0.6.md)。

## L17 · 内层 checkpoint 的 None 可能继承外层存储

- **发现／原因**：研究图嵌入持久化 LangGraph 后，内层默认 `checkpointer=None` 可能继承父图 checkpointer，将消息及来源正文写进原本只应保存控制状态的数据库。
- **处理**：planner 与两个只读子图显式 `checkpointer=False, store=None`；业务正文归原任务库管理。代价是恢复未完成研究要整体重跑，不能宣称子角色独立断点续跑。
- **何时复查**：新增嵌套图或升级 LangGraph／Deep Agents 时，用带父级 SQLite saver 的真实框架回归确认落盘内容，不只检查构造参数。
- **证据**：[delegation.py](../greatsage/delegation.py)、[test_delegation.py](../tests/test_delegation.py) 的 `test_nested_graph_never_inherits_parent_checkpointer`；执行入口见 [v0.7 验证](validation-v0.7.md)。

## L18 · 框架工具 schema 转换会丢掉约束

- **发现／原因**：本轮框架适配中，`StructuredTool → convert_to_openai_tool` 会丢失自有 Pydantic schema 的部分顶层约束，包括 `additionalProperties`；模型可能得到比业务契约更宽的参数定义。
- **处理**：`ProviderDelegationModel.bind_tools()` 对自有结构工具恢复 canonical schema；`_agenerate()` 在框架派发前同时做 JSON Schema 与严格 Pydantic 校验。不要靠模型遵守提示或框架执行时自动修复来保证参数正确。
- **何时复查**：更换 schema 工具、Pydantic 或 LangChain 版本时，检查实际出站定义及非法返回，而不是只看源模型的 `extra="forbid"`。
- **证据**：[delegation.py](../greatsage/delegation.py) 的 `STRICT_SCHEMAS`／`ProviderDelegationModel`、[test_delegation.py](../tests/test_delegation.py)；不据此推定所有云端模型兼容，真实调用结果见 [v0.7 验证](validation-v0.7.md)。

## L19 · 清理正文要覆盖素材重扫与已完成任务

- **审查发现**：只在用户删除前取消任务、只清理活动任务的中间结果，会漏掉素材重新扫描的旧片段，以及完成后仍保留在计划／核对摘要中的原文副本。
- **处理**：collect 绑定 `research_sources(task_id, source_id)`；完成后保留无正文的关联，来源失效事务经 `_redact_events → _invalidate_research_sources` 清 collect／research 结果及可能含原文的 detail，仅留 calls／budget。产物按既有规则保留、引用失效；删除任务再删关联。
- **何时复查**：添加任务详情字段、缓存或新来源更新入口时，逐一确认哪份正文由谁清理；不能把“任务完成”当成“不再依赖来源”。
- **证据**：[task_store.py](../greatsage/task_store.py)、[test_research.py](../tests/test_research.py) 的 `test_material_rescan_clears_research_copies_even_after_completion`，同时覆盖等待审批和已完成任务。

## L20 · 批准后排队仍需要在真正写入入口复核

- **审查发现**：主线程在调用 `to_thread` 前验证身份，不能覆盖线程池排队期间发生的会话／来源／产物变化；审批绑定当前文档也不能保护之后更换的版本。
- **处理**：保存绑定 artifact ID、版本、导出内容哈希及运行身份，逐次点击确认；`TaskManager._call` 的 `execute_local` 在线程实际启动后持存储锁再执行 `pre_execute`，随后工具同锁复核产物并写入。已进入执行后取消仍可能是结果未知，不能将 checkpoint 宣称为 exactly-once。
- **何时复查**：在线程／进程池前后增加 await、调整保存或扩大并发时，将检查点放到真实副作用边界，并使用可控排队夹具。
- **证据**：[tasks.py](../greatsage/tasks.py)、[tools.py](../greatsage/tools.py)、[test_research.py](../tests/test_research.py) 的 `test_worker_entry_rechecks_identity_after_thread_pool_queuing` 和审批版本回归。

## L21 · 重跑阶段不能重置累计预算或抹掉调用证据

- **审查发现**：通用 `_step` 每轮将 detail 清空，研究 resume 会丢失上一轮调用记录，即使任务 state 中的累计预算仍存在。
- **处理**：研究步骤仅保留 `calls/budget` 后重跑，重新生成计划／角色状态；每次模型请求先持久预留累计请求数与输入估算加输出预算，结束后追加用量／结局。累计 active time 在轮次收尾记录，不能声称硬退出前未提交的耗时也能精确恢复。
- **何时复查**：修改 reset／resume、预算和清理策略时，用“已记账调用后失败，再恢复”的一次定向场景检查两轮证据与总额，而不重复性能基准。
- **证据**：[research.py](../greatsage/research.py)、[tasks.py](../greatsage/tasks.py)、[test_research.py](../tests/test_research.py) 的 `test_resume_preserves_accounted_calls_and_cumulative_budget`。最终验证／构建边界见 [validation-v0.7.md](validation-v0.7.md)。

## L22 · 独立框架诊断不能替代真实外层图集成

- **真实故障**：首次 API 探针中，纪要正常，研究在任何模型请求前抛出 `AsyncPregelLoop._put_checkpoint_fut` AttributeError；独立 Deep Agents 诊断和假委派 Runtime 测试都通过。
- **定位与处理**：离线真实 Runtime + LangGraph + 假 provider 复现了内外图组合。临时研究图不能继承持久化外层 Pregel 上下文；在空 `contextvars.Context()` 内创建并直接 await 委派 task，同时保持内层 `checkpointer=False`。不使用 shield，也不放任后台 task；取消／超时沿 await 传播，本地 trace、来源、预算和运行身份通过明确回调传递。
- **何时复查**：修改 SDK 嵌套图、ContextVar、检查点或取消方式时，至少保留一条真实外层图→真实内层框架→脚本化 provider→审批保存的完整路径，不因各层单独通过就推断组合成功。
- **相邻审计缺口**：Skill 本体版本不足以追溯引用文件，研究快照补齐资源的 path/version/truncated 元数据，仍不在默认审计里保存正文。
- **证据**：[research.py](../greatsage/research.py)、[test_research.py](../tests/test_research.py)、[验证记录](validation-v0.7.md)。

## L23 · 当前候选不等于历史实际读取范围

- **审查边界**：任务开始后新增消息、资料重扫或旧版本没有范围快照时，直接重新查询数据库会悄悄改变任务输入；全库候选先截断再过滤，也可能把用户所选资料挤出候选集。
- **处理**：创建时保存所选范围的来源 ID／内容哈希／资料版本；实际选材只在冻结范围内排序，详情从存储实时读取原文。来源变化必须由用户显式更新并创建新任务，旧任务保留。旧 v0.7 任务没有 manifest 时显示当前候选和 stale 标志，不能伪称原执行范围。资料搜索先限制 material IDs，再应用候选上限。
- **范围与取舍**：自动相关选择是有界本地词法评分，不等于全文覆盖或语义召回。全文和固定来源无法满足预算时明确失败；片段保持完整，显示候选上限单独提示，不能用静默截断伪装成功。
- **何时复查**：扩展导入格式、检索器、分页、重选或任务恢复时，同时核对“授权范围”“冻结范围”“实际读到的范围”和“界面展示范围”。
- **证据**：[source_selection.py](../greatsage/source_selection.py)、[materials.py](../greatsage/materials.py)、[test_source_selection.py](../tests/test_source_selection.py)、[test_workbench_research.py](../tests/test_workbench_research.py)。

## L24 · 文档修订要保留编辑对象，但不能把它升级为证据

- **本轮实现审查**：只把纠正附在原研究问题后重跑，会丢失用户正在修改哪份草稿；把旧文档塞进 sources 又会形成模型自证。手工编辑版本的 generating_task_id 为空时，只看当前版本还会丢失最近研究任务与此前纠正范围。
- **处理**：从版本 lineage 查最近生成任务；旧草稿仅作为明确标注的待修改上下文，不分配来源 ID。用户纠正另存 `artifact_correction` 用户消息，固定参加本轮选择；每项结论重新绑定原文证据。同一 artifact 以 expected_version 原子追加版本，记录生成任务／父版本／纠正来源，每版保存重新批准。
- **容量与排序**：共享 `_terms` 只保留 40 项，长旧要求会挤掉后追加的纠正词；本轮将纠正优先放入选材 query。草稿、全局要求和模型工具共同占预算，不能只验草稿长度：草稿 12,000／合并指令 16,000 字符上限之外，还须实际请求预算校验，失败不截断。
- **何时复查**：新增“按意见修改”、手工编辑与自动生成混用、模型上下文扩展或版本比较时，检查编辑对象、事实来源、用户要求三者的不同身份。文本 diff 和有效引用标记都不证明语义正确。
- **证据**：[research.py](../greatsage/research.py)、[artifacts.py](../greatsage/artifacts.py)、[artifact_review.py](../greatsage/artifact_review.py)、[test_workbench_research.py](../tests/test_workbench_research.py)、[test_artifact_revisions.py](../tests/test_artifact_revisions.py)。这些是实现和定向回归入口，不是本版真实模型质量证明。

## L25 · 桌面夹具要检查真正交给 renderer 的代码与可见流程

- **本轮故障**：Node 文件通过 `--check`，但 template literal 内嵌正则的转义在生成 renderer 源码时消失，直到包内运行才报语法错误；随后来源预算断言混用了精确数字与紧凑中文数字格式。
- **处理**：简单路由匹配改用前缀／后缀解析，并用 `vm.Script` 检查烹煮后的 43 段 renderer 模板；来源预算显示精确整数，避免选中量与上限都缩写为“6.4万”时掩盖差距。会话切换后显式返回任务页再操作，不能依赖隐藏按钮的程序点击。
- **何时复查**：修改 `executeJavaScript`／`evaluate` 多层字符串、时间／数字本地化或跨页面夹具时，区分脚本解析、可见页面状态和真实产品结果。只改夹具不重建产品；改界面时仅重建桌面层，复用相同的冻结后端。
- **证据**：[smoke_desktop.cjs](../scripts/smoke_desktop.cjs)、[v0.8 验证](validation-v0.8.md)。
