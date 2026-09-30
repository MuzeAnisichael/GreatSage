# 安全策略

## 支持的版本

GreatSage 处于 alpha 阶段，只对 `main` 分支的最新代码修复安全问题，旧的 alpha 标签不再单独修补。

## 报告漏洞

请不要在公开的 Issue、Pull Request 或讨论中描述漏洞细节。

1. 打开仓库的 **Security** 页，点击 **Report a vulnerability**，通过 GitHub 私密漏洞报告提交。
2. 如果看不到这个入口，请开一个不含任何细节的 Issue，标题写“需要私下报告安全问题”，维护者会提供私下联系方式。

报告中请写明：

- 受影响的版本或提交。
- 复现步骤和影响范围。
- 你认为可行的修复方向（可选）。

不要附上真实的 API 密钥、`connection.json`、录音或私人转写，用虚构数据复现即可。

## 重点关注的问题

- **工具绕过权限层**：例如旁听内容、导入资料、Skill 或工具结果触发了有副作用的操作，或者绕过了点击确认。
- **越出工作目录**：文件工具访问到工作目录之外的路径，包括路径穿越和符号链接。
- **本地接口认证**：本地 HTTP／WebSocket 接口可以绕过认证，或被其他网页、程序调用。
- **凭据泄露**：API 密钥出现在日志、审计快照、导出文件、界面或错误信息中。
- **删除不彻底**：删除或修正后，内容仍能从应用管理的数据中找回。

这是个人维护的 alpha 项目，没有固定的响应时限；维护者会尽快确认并评估。修复发布后，会在变更记录中说明，并在报告者同意时致谢。

---

*English:* please do not disclose vulnerabilities publicly.

- Use **Security → Report a vulnerability** on this repository.
- If that option is missing, open an issue without details asking for a private contact.
- Reproduce with fictional data only, and never include real API keys, `connection.json`, recordings or private transcripts.
