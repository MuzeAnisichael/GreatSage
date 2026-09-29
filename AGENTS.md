# GreatSage development

The user approved the v0.1 implementation plan on 2026-09-04; it was delivered as an alpha. docs/v0.1-plan.md is the historical scope record. See docs/requirements.md for current requirements.
The user approved all six parts of the v0.2 roadmap on 2026-09-27; the mechanisms were delivered as 0.2.0-alpha.1. docs/v0.2-plan.md is the historical scope record and docs/validation-v0.2.md lists measured limits. The separate first-run UI proposal remains deferred.
On 2026-09-29 the user confirmed that v0.3 should center on a source-backed materials-to-minutes/tasks/documents workflow alongside controlled tool execution. The goal is approved for planning; first input/output formats, tool permissions, and detailed interaction still need clarification before implementation. See docs/roadmap.md and docs/requirements.md. Keep planned capabilities distinct from delivered v0.2 behavior.

- Keep this repository independent of its parent workspace and other projects.
- Runtime recordings, transcripts, memories, logs, user Skills and credentials must stay outside version control. Development uses .runtime/.
- Never print API keys. Read existing Windows user environment variables when process environment lacks them.
- Maintain requirements, architecture decisions, validation results and roadmap with behavior changes.
- Record actual test results. Do not label untested latency or source capture as verified.
- v0.1 excludes timed reminders, executing Skill scripts and external computer-control tools.
- Use meaningful commits and push milestones to the user-authorized public GitHub repository.
