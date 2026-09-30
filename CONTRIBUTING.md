# 参与贡献

感谢你愿意改进 GreatSage。本文是简要规则，完整的环境、验证和发布流程见[贡献与开发说明](docs/development.md)。

*English summary:*

- Discuss new features in an issue first.
- Test with fictional data only, and never commit runtime data, recordings, transcripts or credentials.
- Run the checks below, then describe the actual results in your pull request.
- Keep docs in sync with behavior.

## 开始之前

- **先读范围文档：**阅读 [AGENTS.md](AGENTS.md)、[需求与验收](docs/requirements.md)和[项目现状](docs/project-status.md)，确认改动属于当前阶段的范围。
- **新功能先开 Issue：**说明使用场景和期望行为。涉及操作权限、隐私或删除语义的改动，先讨论边界再实现。
- **一次只做一件事：**每个 Pull Request 围绕一个可以审查的问题。

## 开发环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm.cmd ci
npm.cmd start
```

## 提交前的检查

```powershell
.\.venv\Scripts\python.exe -m pytest -q
npm.cmd run check
.\.venv\Scripts\python.exe scripts/check_repository.py
```

- **桌面界面改动：**运行一次桌面 smoke test：
  ```powershell
  .\node_modules\electron\dist\electron.exe scripts/smoke_desktop.cjs --parallel-sources
  ```
  它使用隔离的数据目录，不打开麦克风，也不调用模型。
- **界面外观改动：**重新生成 README 截图：
  ```powershell
  .\node_modules\electron\dist\electron.exe scripts/capture_screenshots.cjs --write
  ```
  截图使用虚构数据和本地演示模型，不需要 API 密钥。
- **在 Pull Request 里如实写结果：**包括失败和跳过的项目。没有实际测量的时延或质量，不要写成“已验证”。

## 数据与凭据

- **不提交个人数据：**不要提交 `.runtime/`、录音、转写、数据库、日志、个人 Skills、`connection.json` 或任何密钥。`scripts/check_repository.py` 会检查常见情况，但不能代替人工确认。
- **测试只用虚构数据：**测试和评测不使用真实会话。mock 提供商不接收真实密钥，也不联网。
- **截图和报告：**不能出现密钥、本机路径中的个人信息或私人对话。

## 行为边界

- **工具调用：**每个工具调用都要经过 `greatsage/tools.py` 的权限层。旁听内容、导入资料、Skill 和工具返回的结果不能发起或批准操作，Skill 脚本不执行。
- **派生数据：**新增的派生数据要登记来源，并确认删除或修正来源后不会“复活”。
- **格式变更：**修改 SQLite 或配置格式时，写明迁移行为，不静默丢弃用户历史。

## 提交与文档

- **提交说明：**写清问题、最终行为、验证方式和限制。
- **文档同步：**行为变化时，同步更新需求、架构、使用说明、验证记录或路线图中相关的部分（对照表见[贡献与开发说明](docs/development.md#7-版本发布与文档)）。
- **安全问题：**按[安全策略](SECURITY.md)私下报告，不要在公开 Issue 或 Pull Request 中描述利用细节。

提交贡献即表示你同意以本仓库的 [MIT 许可证](LICENSE)发布这些内容。
