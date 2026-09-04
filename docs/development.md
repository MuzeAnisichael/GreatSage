# GreatSage 贡献与开发说明

更新日期：2026-09-04。用户已批准 v0.1 开发、使用本地测试服务以及建立 public GitHub 仓库；当前版本为 alpha。

## 1. 开发环境

主要开发基准是 Windows 11、Python 3.11、Node.js/npm。其他 Python 版本与系统的实际可用性尚未单独验收；以发布验证记录为准。

在仓库根目录创建专用环境，避免依赖全局 Python：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm.cmd install
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
| settings.json、secrets-*.bin | 配置快照与 DPAPI 密文文件引用 |
| skills.json | 用户本地 Skill 注册路径与启用状态 |
| models/ | 用户明确准备的模型缓存 |
| recordings/ | 开启录音保存后的 WAV 原文 |
| backend.log、connection.json | 本地进程诊断与临时连接信息 |

独立 Python 服务默认使用 `%LOCALAPPDATA%/GreatSage`，可用 `--data-dir` 覆盖。Electron 打包运行使用其应用数据目录。不要在不同模式下误以为它们自动共享同一份数据库。

`.env.example` 仅列变量名，程序不自动加载 dotenv。已有环境变量和 Windows 用户环境变量可直接使用。UI 密钥使用 DPAPI，不输出、提交或截屏展示明文凭据。

详细审计事件默认 30 天；开启的录音默认 7 天；文本与记忆保留至手动删除。启动诊断日志与审计数据库不是同一机制，排障时分别检查。删除用户数据时还应考虑个人另存的导出与系统备份，不能将本地应用删除宣称为远程服务擦除。

## 3. 日常验证

运行完整自动化回归：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
npm.cmd run check
```

单独验证某一高风险模块，例如：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py tests/test_memory.py -q
```

测试按实际风险组织：取消和过期任务、音源身份、压缩预算、删除级联、修正事务、凭据与路径边界等。新测试应能发现具体退化，不为低影响文档或样式变更编写仅镜像实现的测试。

mock 提供商测试不能接收真实用户密钥，也不应联网。测试临时数据放在测试目录／临时目录，不与 .runtime 共用真实用户历史。

## 4. 真实模型与音频测试

`scripts/probe_providers.py` 提供受控的本地／云端探测参数。先查看帮助，再明确选择要测试的服务和组件：

```powershell
.\.venv\Scripts\python.exe scripts/probe_providers.py --help
```

探测本地模型与云端服务可能触发模型加载、语音合成或付费请求。使用用户授权的服务和测试范围；只记录模型、参数、样本、耗时、结果与必要的用量，不记录凭据。

验证结果至少区分：

- mock 回归：逻辑和故障边界可重复，但不代表语音或模型质量。
- 提供商探测：指定配置能够返回结果，不代表所有接口和模型兼容。
- 原生采集：真实来源能采到预期语音，且来源／格式正确。
- 端到端交互：实际语句结束到文字、语音起播和打断停止的测量。
- 长时间运行：多轮、并发音源、模型失败、跨会话和持续压缩后的稳定性。

语音文件生成时间不能代替实际播放时间；前端占位状态不能算首段有效回答。记录冷／热启动、样本、网络和失败情况，对未达到的性能目标如实报告。

可用 `scripts/benchmark_pipeline.py --tts-provider system --dry-run` 检查合成管线配置，再在授权服务范围内去掉 `--dry-run` 进行真实调用。脚本用固定系统合成语音和独立运行目录，输出断句、ASR、首文及音频 ready 指标，不打开真实音源或播放扬声器。复现方法、原始样本差异及实测限制见[性能基线](performance-baseline.md)。

## 5. 贡献流程

1. 阅读仓库 AGENTS.md、[需求](requirements.md)和相关架构文档，确认变更属于当前版本范围。
2. 新功能先说明使用场景和预期行为；涉及用户需求或操作权限的新边界，先明确范围。
3. 围绕一个可审查的问题修改，实现与文档保持同步。
4. 运行相关回归；仅为具体剩余风险扩大验证，不无边界重复测试。
5. 提交前检查 `git status --short` 和 `git diff --cached`，确认没有运行数据、凭据或个人 Skills。
6. 提交说明写清问题、最终行为、验证和限制；开发期间持续推送有意义的里程碑。

工程保持独立仓库，不把父工作区或其他项目加入提交。不得为了让测试通过删除真实用户数据，也不能用生产会话数据库作为测试 fixture。

## 6. 增加适配器与改动数据格式

- 新服务沿用 providers 层的流式、取消和受控错误接口，不在 UI 中散落凭据。
- 增加配置字段时同步字段校验、默认值、界面和文档；未知字段目前会被拒绝。
- 增加音源时保持来源身份、时间戳和代际失效机制，采集线程不能等待模型调用。
- 增加记忆写入或新的派生数据时登记 source_ids，并验证删除与旧任务提交不能复活内容。
- 变更 SQLite 或配置格式时写明版本和迁移行为，不静默丢弃用户历史。
- Skill 脚本／工具执行不属于本版。未来新增时单独设计调用边界、审计和用户授权，而不是直接执行文档中的命令。

## 7. 版本、发布与文档

Python 与桌面包分别使用对应的 alpha 版本表示。发布时检查二者一致，记录标签、提交、配置变化、迁移说明、已知问题和验证结果。已用 PyInstaller 和 Electron Builder 生成本机目录包；构建流程见 README，实际验证见 [validation.md](validation.md)。目录包不等于跨电脑验证过的安装器。

可重复的桌面 smoke test 使用独立运行目录：

```powershell
.\node_modules\electron\dist\electron.exe scripts/smoke_desktop.cjs --parallel-sources
```

默认只操作本地合成数据和界面，不录音、不调用模型。加 `--live` 才会发送固定的合成模型请求，并用静音播放验证系统语音链路。报告和截图保存在 `.runtime/desktop-smoke/`。`GREATSAGE_DATA_DIR` 可隔离测试与日常数据。

| 文档 | 更新时机 |
| --- | --- |
| README.md、usage.md | 安装、操作、默认值和已知限制变化 |
| requirements.md | 用户确认的范围或验收条件变化 |
| architecture.md | 模块、数据流、边界或调度变化 |
| memory-design.md | 检索、压缩、来源和删除语义变化 |
| roadmap.md | 阶段结束、优先级或范围变化 |
| development.md | 依赖、贡献、测试和发布流程变化 |

最终测试数量、云端模型名单和性能结论以实际验证记录为准，不在工作完成前预填。

## 8. 开源参考与复用

当前架构参考了成熟项目对角色渲染、实时语音调度、打断和可观测性的拆分思路：

- [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber)：桌宠／角色交互与语音体验。
- [AIRI](https://github.com/moeru-ai/airi)：角色、音频管线与 Agent 核心的模块分离。
- [Pipecat](https://github.com/pipecat-ai/pipecat)：实时管线、轮次控制、取消和指标。
- [Microsoft 进程回环示例](https://learn.microsoft.com/en-us/samples/microsoft/windows-classic-samples/applicationloopbackaudio-sample/)：Windows 指定进程采集的官方实现参考。

这些参考不代表本项目继承了其全部能力。直接引入外部代码时记录版本、许可证、修改范围与归属；模型权重、形象素材和语音服务分别核对适用条款。
