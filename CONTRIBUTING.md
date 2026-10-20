# 参与贡献

感谢你愿意改进 GreatSage。本文是简要规则，完整的环境、验证和发布流程见[贡献与开发说明](docs/development.md)。

*English summary:*

- Discuss new features in an issue first.
- Test with fictional data only, and never commit runtime data, recordings, transcripts or credentials.
- Select checks for the affected behavior, reuse valid evidence, and report actual results; existing CI gates still apply.
- Keep docs in sync with behavior.

## 开始之前

- **先读范围文档**：从 [AGENTS.md](AGENTS.md)进入[开发方法指导](agent.md)，按[代码导航](docs/code-map.md)定位功能，确认改动属于当前阶段的范围；相关历史问题见[经验记录](docs/lessons-learned.md)。
- **新功能先开 Issue**：说明使用场景和期望行为。涉及操作权限、隐私或删除语义的改动，先讨论边界再实现。
- **一次只做一件事**：每个 Pull Request 围绕一个可以审查的问题。

## 开发环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm.cmd ci
npm.cmd start
```

## 按改动选择检查

先说明本次可能影响什么，再选择最接近该行为的现有检查。文档改动检查链接与符号；机械改名检查引用／语法；行为变更运行对应模块测试。权限、删除、取消与恢复的相关失败路径仍须覆盖。完整策略见[开发方法指导](agent.md)。

例如，只涉及记忆与任务存储时：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_memory.py tests/test_tasks.py -q
```

跨模块集成或发布需要完整回归时，使用：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
npm.cmd run check
.\.venv\Scripts\python.exe scripts/check_repository.py
```

- **桌面交互链变化**：需要检查窗口、播放或任务联动时运行一次桌面 smoke test：
  ```powershell
  .\node_modules\electron\dist\electron.exe scripts/smoke_desktop.cjs --parallel-sources
  ```
  它使用隔离的数据目录，不打开麦克风，也不调用模型。
- **公开截图已不能反映界面时**：重新生成 README 截图：
  ```powershell
  .\node_modules\electron\dist\electron.exe scripts/capture_screenshots.cjs --write
  ```
  截图使用虚构数据和本地演示模型，不需要 API 密钥。
- **在 Pull Request 里如实写结果**：包括失败和跳过的项目。没有实际测量的时延或质量，不要写成“已验证”。
- **避免重复**：可以引用适用于本次代码／配置的现有结果并说明覆盖边界，不要求每个切片重复全量测试、打包或真实模型调用；CI 与发布门槛仍保留。

## 数据与凭据

- **不提交个人数据**：不要提交 `.runtime/`、录音、转写、数据库、日志、个人 Skills、`connection.json` 或任何密钥。`scripts/check_repository.py` 会检查常见情况，但不能代替人工确认。
- **测试只用虚构数据**：测试和评测不使用真实会话。mock 提供商不接收真实密钥，也不联网。
- **截图和报告**：不能出现密钥、本机路径中的个人信息或私人对话。

## 行为边界

- **工具调用**：每个工具调用都要经过 `greatsage/tools.py` 的权限层。旁听内容、导入资料、Skill 和工具返回的结果不能发起或批准操作，Skill 脚本不执行。
- **派生数据**：新增的派生数据要登记来源，并确认删除或修正来源后不会“复活”。
- **格式变更**：修改 SQLite 或配置格式时，写明迁移行为，不静默丢弃用户历史。

## 提交与文档

- **提交说明**：写清问题、最终行为、验证方式和限制。
- **文档同步**：行为变化时，同步更新需求、架构、使用说明、验证记录或路线图中相关的部分（对照表见[贡献与开发说明](docs/development.md#7-版本发布与文档)）。
- **安全问题**：按[安全策略](SECURITY.md)私下报告，不要在公开 Issue 或 Pull Request 中描述利用细节。

提交贡献即表示你同意以本仓库的 [MIT 许可证](LICENSE)发布这些内容。
