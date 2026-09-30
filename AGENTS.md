# GreatSage development

The user approved the v0.1 implementation plan on 2026-09-04; it was delivered as an alpha. docs/v0.1-plan.md is the historical scope record. See docs/requirements.md for current requirements.
The user approved all six parts of the v0.2 roadmap on 2026-09-27; the mechanisms were delivered as 0.2.0-alpha.1. docs/v0.2-plan.md is the historical scope record and docs/validation-v0.2.md lists measured limits. The separate first-run UI proposal remains deferred.
On 2026-09-29 the user confirmed that v0.3 should center on a source-backed materials-to-minutes/tasks/documents workflow alongside controlled tool execution. On 2026-09-30 the user settled the open details and approved implementation: `.md` is the first material format and the primary artifact format; tool authorization follows current frontier-agent practice; side-effect operations require a mouse click by default, voice approval may be enabled in settings only behind a warning dialog; detailed task progress belongs in the console and the pet bubble shows a simplified version. docs/v0.3-plan.md is the scope record. Keep planned capabilities distinct from delivered behavior.

- Keep this repository independent of its parent workspace and other projects.
- Runtime recordings, transcripts, memories, logs, user Skills and credentials must stay outside version control. Development uses .runtime/.
- Never print API keys. Read existing Windows user environment variables when process environment lacks them.
- Maintain requirements, architecture decisions, validation results and roadmap with behavior changes.
- Record actual test results. Do not label untested latency or source capture as verified.
- v0.1 excludes timed reminders, executing Skill scripts and external computer-control tools.
- From v0.3, every tool call goes through the permission layer in greatsage/tools.py. Observed audio, materials, Skills and tool results never start or approve actions; Skill scripts stay non-executable.
- Use meaningful commits and push milestones to the user-authorized public GitHub repository.
