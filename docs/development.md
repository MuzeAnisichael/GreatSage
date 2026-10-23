# GreatSage 贡献与开发说明

更新日期：2026-10-24。当前交付版本为从 `main` 发布的 **0.6.0-alpha.1**，新增 LangMem 提取、三档确认、候选／实体关系与来源保护，保留连续语音、单源恢复和此前的资料／工具／框架迁移，提供 Windows x64 ZIP 与实验性 NSIS。[项目现状](project-status.md)区分已实现、已验证和仍待真实验收；[开发日志](development-log.md)随每个模块提交维护。简要贡献规则见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

## 1. 开发环境

主要开发基准是 Windows 11、Python 3.11、Node.js/npm。其他 Python 版本与系统的实际可用性尚未单独验收；以发布验证记录为准。

在仓库根目录创建专用环境，避免依赖全局 Python：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm.cmd ci
npm.cmd start
```

需要本地 ASR 或 Windows 系统语音时安装可选依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[local]"
```

`scripts/setup.ps1` 安装开发与本地语音依赖。识别模型权重仍需在设置页面明确准备；不要把“开始监听”改成隐式联网下载模型。

Electron 启动开发环境下的 Python 后端并管理本地连接。单独开发服务可运行：

```powershell
.\.venv\Scripts\python.exe -m greatsage --data-dir .runtime --port 8765
```

独立服务需要客户端提供它的本地连接凭据；直接打开网页不等同于已认证的桌面会话。不要在问题报告中粘贴 connection.json。

## 2. 运行数据与凭据

| 位置 | 内容 |
| --- | --- |
| .runtime/ | 源码桌面运行数据，已从版本控制排除 |
| memory.sqlite3 | 历史、记忆、摘要、依赖关系和审计事件 |
| agent-checkpoints.sqlite3 | LangGraph 控制状态；正文与工具执行账本仍在 memory.sqlite3 |
| settings.json、secrets-*.bin | 配置快照与 DPAPI 密文文件引用 |
| skills.json | 用户本地 Skill 注册路径与启用状态 |
| models/ | 用户明确准备的模型缓存 |
| recordings/ | 开启录音保存后的 WAV 原文 |
| backend.log、connection.json | 本地进程诊断与临时连接信息 |

独立 Python 服务默认使用 `%LOCALAPPDATA%/GreatSage`，可用 `--data-dir` 覆盖。Electron 打包运行使用其应用数据目录。不要在不同模式下误以为它们自动共享同一份数据库。

`.env.example` 仅列变量名，程序不自动加载 dotenv。已有环境变量和 Windows 用户环境变量可直接使用。UI 密钥使用 DPAPI，不输出、提交或截屏展示明文凭据。

详细审计事件默认 30 天；开启的录音默认 7 天；文本与记忆保留至手动删除。启动诊断日志与审计数据库不是同一机制，排障时分别检查。删除用户数据时还应考虑个人另存的导出与系统备份，不能将本地应用删除宣称为远程服务擦除。

## 3. 日常验证

按 [agent.md](../agent.md) 的验证决策表选择最小充分检查。先用[代码导航](code-map.md)找到对应测试；文档只核对链接和描述，机械整理检查引用／导入，行为变更运行相关用例。只在跨模块风险或发布门槛需要时扩大范围，已有适用的 CI／报告可直接引用。

需要完整自动化回归时：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
npm.cmd run check
.\.venv\Scripts\python.exe scripts/check_repository.py
```

单独验证某一高风险模块，例如：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py tests/test_memory.py -q
```

测试按实际风险组织：取消和过期任务、音源身份、压缩预算、删除级联、修正事务、凭据与路径边界等。新测试应能发现具体退化，不为低影响文档或样式变更编写仅镜像实现的测试。

mock 提供商测试不能接收真实用户密钥，也不应联网。测试临时数据放在测试目录／临时目录，不与 .runtime 共用真实用户历史。

迁移后的模型／MCP／任务图还可以通过真实回环 HTTP 做黑盒检查，不需要外部服务、密钥或麦克风：

```powershell
.\.venv\Scripts\python.exe scripts/smoke_backend.py
.\.venv\Scripts\python.exe scripts/smoke_backend.py --executable dist/greatsage-backend/greatsage-backend.exe
```

第一条已加入 Windows CI；第二条在冻结构建后检查动态依赖。脚本用独立目录和合成服务，验证请求、逐次审批、进程中断后重新授权、工具不重复执行、纪要／待办／文档和监控元数据。原始报告在 `.runtime/backend-smoke/`。它主动终止自己启动的测试后端来模拟中断，不代替桌面正常退出或安装器验收。

## 4. 真实模型与音频测试

`scripts/probe_providers.py` 提供受控的本地／云端探测参数。先查看帮助，再明确选择要测试的服务和组件：

```powershell
.\.venv\Scripts\python.exe scripts/probe_providers.py --help
```

探测本地模型与云端服务可能触发模型加载、语音合成或付费请求。使用用户授权的服务和测试范围；只记录模型、参数、样本、耗时、结果与必要的用量，不记录凭据。

只有需要核实真实原生工具调用或 MCP 互操作时，使用 `scripts/probe_agent_tools.py`。它读取既有配置／环境凭据，在隔离目录中每种配置最多发出两次模型请求、批准一个预期工具；脚本调用应用的点击审批入口，不模拟物理鼠标，也不打开麦克风。

```powershell
.\.venv\Scripts\python.exe scripts/probe_agent_tools.py --cloud --mcp-learn
.\.venv\Scripts\python.exe scripts/probe_agent_tools.py --ollama --local-model qwen3:8b
```

云端示例要求现有 LLM 配置为 OpenRouter，只查询 Microsoft Learn 的公共文档；本地示例只读取临时生成的虚构文件。可用 `--settings-dir` 选择配置目录、`--cloud-model` 指定云端模型，更多参数见 `--help`。默认配置目录为 `.runtime`，Ollama 使用本机 `127.0.0.1:11434`。报告保存在 `.runtime/probe-agent-tools/`；一次通过不代表该模型的普遍能力，实际结果见[v0.4 验证](validation-v0.4.md)。不要把这个按需检查加入每次提交的默认测试。

验证结果至少区分：

- mock 回归：逻辑和故障边界可重复，但不代表语音或模型质量。
- 提供商探测：指定配置能够返回结果，不代表所有接口和模型兼容。
- 原生采集：真实来源能采到预期语音，且来源／格式正确。
- 端到端交互：实际语句结束到文字、语音起播和打断停止的测量。
- 长时间运行：多轮、并发音源、模型失败、跨会话和持续压缩后的稳定性。

语音文件生成时间不能代替实际播放时间；前端占位状态不能算首段有效回答。记录冷／热启动、样本、网络和失败情况，对未达到的性能目标如实报告。

可用 `scripts/benchmark_pipeline.py --tts-provider system --dry-run` 检查合成管线配置，再在授权服务范围内去掉 `--dry-run` 进行真实调用。脚本用固定系统合成语音和独立运行目录，输出断句、ASR、首文及音频 ready 指标，不打开真实音源或播放扬声器。复现方法、原始样本差异及实测限制见[性能基线](performance-baseline.md)。

## 5. 贡献流程

1. 阅读仓库 [AGENTS.md](../AGENTS.md)和[方法指导](../agent.md)，按[代码导航](code-map.md)定位功能、检索[经验记录](lessons-learned.md)，确认变更属于当前版本范围。
2. 新功能先说明使用场景和预期行为；涉及用户需求或操作权限的新边界，先明确范围。
3. 围绕一个可审查的问题修改，实现与文档保持同步。
4. 运行相关回归；仅为具体剩余风险扩大验证，不无边界重复测试。
5. 每个切片提交前检查 `git status --short`、`git diff --cached`；本轮推送前运行仓库检查脚本，确认没有运行数据、凭据或个人 Skills。
6. 提交说明写清问题、最终行为、验证和限制；开发期间持续推送有意义的里程碑。

工程保持独立仓库，不把父工作区或其他项目加入提交。不得为了让测试通过删除真实用户数据，也不能用生产会话数据库作为测试 fixture。

## 6. 增加适配器与改动数据格式

- 新服务沿用 providers 层的流式、取消和受控错误接口，不在 UI 中散落凭据。
- 聊天适配复用 `chat_models.py` 的 LangChain 入口；升级固定版本时重点核对费用字段、Ollama 工具名称、取消、错误脱敏和隐式 tracing。任务图控制状态不得复制业务正文，所有本地／MCP 工具必须经过 `tools.py`。
- 增加配置字段时同步字段校验、默认值、界面和文档；未知字段目前会被拒绝。
- 增加音源时保持来源身份、时间戳和代际失效机制，采集线程不能等待模型调用。
- 增加记忆写入或新的派生数据时登记 source_ids，并验证删除与旧任务提交不能复活内容。
- 变更 SQLite 或配置格式时写明版本和迁移行为，不静默丢弃用户历史。
- v0.3 起，每个工具调用都经过 `greatsage/tools.py` 的权限层。新增工具时声明效果分级、参数校验和审计内容；旁听内容、资料、Skill 和工具结果不能发起或批准操作，Skill 脚本仍不执行。资料任务还要记录产物依赖的来源、版本及修正／删除后的失效方式。

## 7. 版本、发布与文档

Python 与桌面包分别使用对应的 alpha 表示：当前为 `0.6.0a1` 与 `0.6.0-alpha.1`。发布时同步 `greatsage/__init__.py`、`pyproject.toml`、`package.json` 和 `package-lock.json`，记录标签、提交、配置变化、迁移说明、已知问题和验证结果。`main` 是交付主干；模块切片经检查后合入，版本标签指向交付提交。

PyInstaller 冻结后端，electron-builder 生成桌面目录、ZIP 和 NSIS。`scripts/build.ps1` 默认 `-Target dir`，单一产物可用 `-Target zip/nsis`；若同时交付两种格式，后端构建一次后执行 `electron-builder --win zip nsis --x64 --publish never`，避免重复冻结。将整个目录分发，不能只复制 `GreatSage.exe`。[v0.6.0-alpha.1 预发布](https://github.com/MuzeAnisichael/GreatSage/releases/tag/v0.6.0-alpha.1)提供 Windows x64 ZIP、实验性安装器和 `SHA256SUMS.txt`。发布前记录构建代码提交、两个产物的大小／SHA256，上传后核对远端资产；无受信任发行签名，人工安装／升级／卸载仍待验收。

此前构建遇到 Electron 下载缓存重命名权限错误，后端已成功，因此只重试桌面阶段并复用已安装且版本一致的 Electron：`.\node_modules\.bin\electron-builder.cmd --win zip nsis --x64 --publish never --config.electronDist=node_modules/electron/dist`。这不是默认必需参数；先确认失败阶段和本地 Electron 版本，再决定是否使用，避免重复构建整个后端。

本版继续使用原 `settings.json` 格式、`memory.sqlite3` 和 v0.4 加入的 `agent-checkpoints.sqlite3`；v0.6 以新增表保存结构记忆、来源处理进度、超预算延后与抑制记录，已有文本记录保留。运行目录的 `memory-extraction-state.json` 仅保存安全错误码和设置版本，用于跨重启保持提取的 `needs_attention` 暂停，不包含候选或原文正文。升级前退出应用并备份完整数据目录，包括配置引用的密文文件；不要只复制主 SQLite 文件而遗漏仍存在的 WAL。DPAPI 凭据绑定当前 Windows 用户，复制到另一台电脑不等同于凭据可用。替换程序目录时保留原数据，验证边界见[v0.6 验证](validation-v0.6.md)。

可重复的桌面 smoke test 使用独立运行目录：

```powershell
.\node_modules\electron\dist\electron.exe scripts/smoke_desktop.cjs --parallel-sources
```

默认只操作本地合成数据和界面，不录音、不调用模型。构建后使用 `--packaged` 读取 `release/win-unpacked` 中的桌面代码和冻结后端，避免拿源码通过代替构建验收。加 `--live` 才会发送固定的合成模型请求，并用静音播放验证系统语音链路。报告和截图保存在 `.runtime/desktop-smoke/`。`GREATSAGE_DATA_DIR` 可隔离测试与日常数据。

README 截图和社交预览图用下面的脚本重新生成：

```powershell
.\node_modules\electron\dist\electron.exe scripts/capture_screenshots.cjs --write
```

它的工作方式：

- **数据**：用隔离的数据目录启动桌面应用，写入虚构的会议、资料和记忆。
- **模型**：由脚本内置的本地演示模型回答；首个回复块按实测的中位时延延迟，避免时延卡片显示不真实的数值。
- **流程**：纪要任务、审批和桌宠都走真实流程。不需要 API 密钥，不访问网络，也不打开麦克风。
- **输出**：截图先写入 `.runtime/screenshots/`；加 `--write` 才覆盖 `docs/assets/`。会显示本机路径的页面（资料列表、工作目录）不放进截图。

仓库的社交预览图 `docs/assets/social-preview.png` 需要维护者在 GitHub 仓库设置中手动上传。

| 文档 | 更新时机 |
| --- | --- |
| README.md、project-status.md、usage.md | 安装、操作、当前交付状态和已知限制变化 |
| requirements.md | 用户确认的范围或验收条件变化 |
| architecture.md | 模块、数据流、边界或调度变化 |
| memory-design.md | 检索、压缩、来源和删除语义变化 |
| roadmap.md | 阶段结束、优先级或范围变化；标出已确认目标与仍待设计的细节 |
| development.md | 依赖、贡献、测试和发布流程变化 |
| agent.md、AGENTS.md | 开发方法或项目约束变化；AGENTS.md 保持入口，方法只维护一份 |
| code-map.md | 文件／符号／功能入口变化；同步结构树、功能表与相关调用链 |
| development-log.md、lessons-learned.md | 每个切片的结果；新增或修正有证据的故障原因、复用经验 |

最终测试数量、云端模型名单和性能结论以实际验证记录为准，不在工作完成前预填。

## 8. 开源参考与复用

v0.5 采用 Pipecat 1.13.0 与 Deepgram SDK 7.14.0；冻结时只补必经的懒加载入口和元数据，不收集所有服务 extras 或模型。许可与来源见 [third-party.md](third-party.md)。NSIS 使用 `scripts/build.ps1 -Target nsis`，当前源码版本为 `0.6.0a1`／`0.6.0-alpha.1`，发布状态以[项目现状](project-status.md)为准。

升级前从托盘菜单退出 GreatSage，再备份完整数据目录。NSIS 的运行中升级处理可能强制结束进程，不保证执行应用关闭流程。默认当前用户安装、手动升级、卸载保留数据、不自动启动；DPAPI 仍绑定 Windows 用户。`GREATSAGE_DATA_DIR` 显式设置时也隔离 Electron profile，正常启动沿用既有路径。

有界退出复现：`python scripts/smoke_shutdown.py`；冻结包可追加 `--executable dist/greatsage-backend/greatsage-backend.exe`。实际安装验收脚本为 [smoke_installer.ps1](../scripts/smoke_installer.ps1)，带人工退出节点，先阅读预检与注册项／快捷方式／缓存边界。重复安装相同版本只能证明覆盖安装，不等于跨版本升级通过。完整安装、自然语音和整天运行的未测项见[v0.5 验证](validation-v0.5.md)。

当前架构参考了成熟项目对角色渲染、实时语音调度、打断和可观测性的拆分思路：

- [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber)：桌宠／角色交互与语音体验。
- [AIRI](https://github.com/moeru-ai/airi)：角色、音频管线与 Agent 核心的模块分离。
- [Pipecat](https://github.com/pipecat-ai/pipecat)：实时管线、轮次控制、取消和指标。
- [Microsoft 进程回环示例](https://learn.microsoft.com/en-us/samples/microsoft/windows-classic-samples/applicationloopbackaudio-sample/)：Windows 指定进程采集的官方实现参考。

这些参考不代表本项目继承了其全部能力。直接引入外部代码时记录版本、许可证、修改范围与归属；模型权重、形象素材和语音服务分别核对适用条款。

## v0.6 记忆提取开发

依赖固定 LangMem／trustcall，模型走既有 Providers，禁止由提取模型直接写库。定位入口见[代码导航](code-map.md)，范围见[v0.6 计划](v0.6-plan.md)。本地只跑受影响检查；全仓检查交由 Windows CI。冻结后运行 `greatsage-backend.exe --check-memory-runtime`（源码用 `python -m greatsage --check-memory-runtime`）验证真实 LangMem 加载与一次合成提案，不读取用户设置、不联网。新表升级前备份完整数据目录；不承诺旧版本降级后理解新候选。

提取的确定性错误直接进入 `needs_attention`；暂时性错误最多尝试三次（含首次）后暂停。`Runtime._set_extraction_attention()` 原子写入上述安全标记，同配置下重启不自动重发；手动提取、后台重试或配置更新可恢复。不要用删除该文件作为常规故障恢复步骤，界面入口会同时处理重试状态。
