# Project Guidelines (Copilot IDE Environment)

> This file is for the local VS Code workspace at `c:/Users/Administrator/Desktop/fbpost`.
> The primary codebase is this folder.

---

## Architecture

Single Python automation project for Facebook group posting workflows.

```text
fbpost/
├── core/
│   ├── session_manager.py
│   ├── group_scraper.py
│   ├── post_queue.py
│   ├── scheduler.py
│   └── logger.py
├── data/
│   ├── session.json
│   ├── groups.json
│   ├── posted_log.json
│   └── blacklist.txt
├── templates/
│   ├── post_1.yaml
│   ├── post_2.yaml
│   └── images/
├── docs/
├── logs/
├── config.yaml
├── main.py
└── requirements.txt
```

Automation flow:
1. `session_manager.py` prepares/refreshes login session.
2. `group_scraper.py` builds/updates joined group inventory.
3. `post_queue.py` resolves template rotation and payload.
4. `scheduler.py` executes paced posting loops and logging.

---

## Camoufox Stack

Core runtime:
- Python 3.10+
- `camoufox[geoip]`
- Playwright-compatible browser control
- YAML/JSON file persistence for local state

Install and bootstrap:

```bash
pip install -U -r requirements.txt
camoufox fetch
python main.py --setup
python main.py --run-once
```

Useful Camoufox CLI:
- `camoufox fetch`
- `camoufox path`
- `camoufox test`
- `camoufox remove`
- `python -m camoufox version`

---

## Build and Validation

Use lightweight Python validation for each change:

```bash
python -m compileall main.py core
python main.py --setup
python main.py --run-once
```

If tests are added later, run them before completion.

## Python Environment Policy (Hard Requirement)

- Do not create, configure, select, or reference any virtual environment for this repository.
- Do not run `venv`, `.venv`, `virtualenv`, `pipenv`, `poetry`, `conda`, `pyenv`, or any environment bootstrap command.
- Use system Python only for all commands and validation steps.
- If a tool suggests environment setup, skip it and continue with system Python commands directly.
- Do not run environment setup/check tools for this repository (for example `configure_python_environment`) unless the user explicitly requests it.
- Do not run any `.venv` existence checks or related commands unless the user explicitly requests them.

---

## Runtime and Logging

- Output logs are written to `logs/session_YYYYMMDD.log`.
- Persistent data lives in `data/`.
- Do not commit private session artifacts.

---

## Code Style

- Python with type hints.
- Prefer small, testable functions.
- Keep selectors/config centralized and easy to update.
- Keep behavior deterministic where possible; randomness should be explicit and configurable.
- Use UTF-8 file encoding.

---

## Project Conventions

### Session Handling

- Reuse saved session when valid.
- Detect login redirect/session invalidation and re-enter setup flow.
- Save refreshed session state at end of each successful run.

### Group Inventory

- `groups.json` is the source of truth for active/inactive targets.
- Merge updates; avoid destructive overwrite of user metadata.

### Posting and Queue

- Templates are loaded from `templates/post_*.yaml`.
- Rotation mode is controlled by `config.yaml`.
- Respect per-group cooldown and per-session caps.

### Scheduler

- Enforce random delay between `min_delay_minutes` and `max_delay_minutes`.
- Enforce periodic long breaks after `rest_every_n_posts`.
- Log all outcomes: posted, skipped, failed.

---

## Integration Points

| Integration | Purpose | Notes |
|---|---|---|
| Facebook Web UI | Posting target | Subject to UI changes and restrictions |
| Camoufox | Browser automation and anti-detection support | Use documented parameters only |
| Optional proxy | Network/geo alignment | Use `geoip` when proxy is enabled |

---

## Security

- Never commit `.env` or credentials.
- Keep session files local and machine-specific.
- Redact sensitive data in logs.
- Avoid hardcoding proxy credentials in source files.

---

## Data Model

Local file-based persistence:

- `data/session.json`: Browser/session state
- `data/groups.json`: Group inventory + status
- `data/posted_log.json`: Posting history + timestamps
- `data/blacklist.txt`: Manual skip list

---

## ⛔ MANDATORY FIRST STEP — Read Before ANY Action

> STOP. Before writing code, making edits, or running commands, read the relevant local instructions and task context for this project.

1. Read this file: `.github/copilot-instructions.md`.
2. If present, read `.claude/tasks/lessons.md`.
3. If present, scan `.claude/skills/` for matching skills.
4. Re-evaluate required reads at each major to-do step.

If a referenced path does not exist, continue with the next applicable instruction.

### ⛔ MANDATORY AFTER EVERY CHANGE — Commit + Push

> After each logical change set:
> 1. Stage (`git add -A`)
> 2. Commit with a conventional message
> 3. Push to the active branch remote

If the repository is not initialized or no remote exists, document that limitation in your final report.

### ⛔ MANDATORY AFTER CODE CHANGES — Validate

> After code changes (not docs-only):
> 1. Run syntax/compile validation (`python -m compileall main.py core`)
> 2. Run a smoke command (`python main.py --run-once`)
> 3. Confirm no new runtime errors in logs

---

## Agent Behaviour Rules

### 0. Execution Discipline (User Hard Rule)

- Do not add features, options, or behavior changes unless the user explicitly asks for them.
- If the user asks a question, answer the question only. Do not run commands, edit files, or perform side effects unless the user explicitly asks for action.
- Before any non-trivial code change, state: exact change, exact reason tied to user request, and impact scope.
- Keep changes minimal and PRD-aligned; avoid assumption-based improvements.
- If a possible change is optional, ask for approval before implementing it.
- Browser/runtime behavior must remain config-driven. Do not hardcode scheduler overrides for browser options that already exist in `config.yaml`.
- For group posting flow, do not introduce background scroll/motion activity after composer popup opens unless explicitly requested by user.

### 1. No Lazy Fixes
- Always find and fix root causes. Never apply temporary workarounds or band-aids.
- When fixing a file, check all other files that import from or depend on the changed code. Trace the full impact.
- Senior developer standards: would a staff engineer approve this change?

### 2. Strict Type Safety
- Avoid unsafe typing patterns and unnecessary casts.
- Keep data structures consistent with project schemas and config contracts.
- If a type mismatch exists, fix definitions or data flow instead of bypassing type checks.

### 3b. Generated Documents — Always in `/docs` at Project Root
- All generated markdown documents (reports, deep dives, PRDs, implementation plans, audits, architecture docs) must be saved to `docs/` at the project root.
- Use descriptive filenames with date prefix when relevant.
- Create the `docs/` folder if it does not exist.

### 4. Workflow Orchestration

Plan mode:
- Enter plan mode for non-trivial tasks (3+ steps or architectural decisions).
- Write plan to `.claude/tasks/todo.md` if that workflow exists in this repository.
- If something goes sideways, stop and re-plan.

Subagent strategy:
- Use subagents for research/exploration when helpful.

Verification before done:
- Never mark a task complete without proving it works.
- Run syntax checks and smoke commands.

Autonomous bug fixing:
- When given a bug report, identify cause and resolve with minimal hand-holding.

### 5. Task Management

1. Write plan to `.claude/tasks/todo.md` when available.
2. Mark items complete as you go.
3. Add a short review section when done.
4. Update lessons after user corrections when the lessons file exists.

### 6. Git Discipline

- Scope: Only commit and push inside this repository (`fbpost/`).
- Do not touch unrelated repositories/folders.
- Commit after each logical change set.
- Use conventional commit messages.

Recommended flow:
1. `git add -A`
2. `git commit --no-verify -m "<type(scope): subject>"`
3. `git push`

### 7. External Service Interaction Rule
- Centralize external interactions (Facebook automation, proxies, browser setup) in core modules.
- Avoid scattered ad-hoc calls across unrelated files.
- Keep integration boundaries explicit and documented.

### 8. Core Principles

- Simplicity first: minimal, clear changes.
- No laziness: fix root causes.
- Minimal impact: touch only what is necessary.
- Full traceability: ensure dependent flows still work.
