# Project Guidelines

Facebook Group Auto-Poster: multi-account automation for posting to Facebook groups using Camoufox (anti-detection Firefox).

---

## ⛔ MANDATORY FIRST STEP — Read Before ANY Action

> **STOP. Before writing ANY code, making ANY edit, or running ANY command, you MUST do these reads FIRST.**
> This is not optional. Skipping this leads to incorrect approaches and wasted effort.
> **This applies to EVERY user message — not just the first one in a session.**

1. **Read `.github/lessons.md`** — Hard-won rules from past mistakes. Every principle is a blocking constraint.
2. **Scan `.github/skills/`** — Identify which skill files match your current task and read their SKILL.md files.
   - Committing code → `git-commit` + `conventional-commit`
   - Writing a PRD → `prd`
   - Refactoring → `refactor`
   - Any task → check if a matching skill exists first
3. **Re-evaluate at each to-do item** — Different steps may need different skills.

**If you find yourself about to edit a file without having read lessons + relevant skills first, STOP and read them.**

### ⛔ MANDATORY AFTER EVERY CHANGE — Commit + Push

> **After EVERY change — code, docs, config, lessons, instructions, ANYTHING — you MUST immediately:**
> 1. **Stage** — `git add -A`
> 2. **Commit** — `git commit --no-verify` with a conventional commit message
> 3. **Push** — `git push`
>
> **This applies to ALL files in the repo. A one-line docs edit gets committed and pushed immediately.**
> **Do NOT wait for the user to remind you. This is automatic. No exceptions.**

### ⛔ MANDATORY AFTER CODE CHANGES — Validate + Test

> **After changes to Python code (NOT docs/config-only changes), you MUST also:**
> 1. **Compile check** — `python -m compileall main.py core` to verify no syntax errors
> 2. **Dry run** — `python main.py --run-once --dry-run` when relevant to verify logic
>
> **This is SEPARATE from commit+push. Both must happen. Validation verifies the code works. Commit+push saves it.**

---

## Agent Behaviour Rules

### 1. No Lazy Fixes
- Always find and fix root causes. Never apply temporary workarounds or band-aids.
- When fixing a file, check all other files that import from or depend on the changed code. Trace the full impact.
- Senior developer standards: would a staff engineer approve this change?

### 2. Strict Type Safety
- **All functions use Python type hints.** Use `from __future__ import annotations` at top of modules.
- If a type mismatch exists, fix the type definition or the data flow — never use `# type: ignore` without justification.

### 3. Generated Documents — Always in `/docs` at Project Root
- **ALL generated markdown documents** (reports, deep dives, PRDs, implementation plans, audits, architecture docs) MUST be saved to the `/docs` folder at the project root, **NOT** scattered elsewhere.
- Use descriptive filenames with date prefix when relevant: e.g., `docs/2026-03-06-scheduler-audit.md`.
- Create the `/docs` folder if it doesn't exist.

### 4. Workflow Orchestration

**Plan Mode**: Enter plan mode for any non-trivial task (3+ steps or architectural decisions). Write plan to `.github/tasks/todo.md` with checkable items. If something goes sideways, STOP and re-plan immediately.

**Subagent Strategy**: Use subagents liberally for research, exploration, and parallel analysis. One task per subagent. Keep main context window clean.

**Self-Improvement Loop**: After ANY correction from the user, update `.github/lessons.md` with the pattern and a rule to prevent recurrence. Review lessons at session start.

**Verification Before Done**: Never mark a task complete without proving it works. Run compile check, test with `--dry-run`, demonstrate correctness.

**Demand Elegance (Balanced)**: For non-trivial changes, pause and consider if there's a more elegant approach. Skip for simple, obvious fixes — don't over-engineer.

**Autonomous Bug Fixing**: When given a bug report, just fix it. Point at logs/errors/failing tests, then resolve. Zero hand-holding required from the user.

### 5. Task Management

1. Write plan to `.github/tasks/todo.md` with checkable items
2. Mark items complete as you go
3. High-level summary at each step
4. Add review section to `.github/tasks/todo.md` when done
5. Update `.github/lessons.md` after corrections

### 6. Git Discipline

- **Commit After Every Change**: After every change — even a single-line fix — immediately stage, commit, and push. No batching multiple unrelated changes. Keep the remote always up to date.
- **Use Git Skills**: Before committing, read and follow the relevant Git skills in `.github/skills/` (e.g., `git-commit`, `conventional-commit`). Generate conventional commit messages with proper type, scope, and description.
- **Never commit `data/` or `logs/`**: Session files and log files are machine-specific and in `.gitignore`.

### 7. Core Principles

- **Simplicity First**: Make every change as simple as possible. Minimal code impact.
- **No Laziness**: Find root causes. No temporary fixes.
- **Minimal Impact**: Changes touch only what's necessary. Avoid introducing bugs.
- **Full Traceability**: When changing shared code in `core/`, verify all consumers still work.
- **Config-Driven**: All runtime behavior comes from `config.yaml`. No hardcoded overrides.

---

## Architecture

```
poster/
├── main.py                    # CLI entry point (argparse)
├── config.yaml                # Global config + per-account overrides
├── core/
│   ├── session_manager.py     # Camoufox browser lifecycle, login, session persistence
│   ├── group_scraper.py       # Scrapes joined groups from Facebook
│   ├── post_queue.py          # Template loading and rotation (single/sequential/round-robin/random)
│   ├── scheduler.py           # Main posting loop with delays and cooldowns
│   ├── account_manager.py     # Multi-account CRUD operations
│   ├── web_ui.py              # Built-in HTTP control panel (no framework)
│   ├── config_loader.py       # YAML config loading with account merging
│   └── logger.py              # Structured logging
├── templates/                 # Post templates (post_*.yaml) + images/
└── data/accounts/<id>/        # Per-account session, groups, logs
```

**Automation flow:**
1. `session_manager` → Camoufox browser with persistent profile, handles login/session
2. `group_scraper` → Extracts joined groups, merges with existing inventory
3. `post_queue` → Selects template based on rotation mode
4. `scheduler` → Posts to groups with random delays, cooldowns, rest breaks

**Multi-account:** Each account under `config.yaml > accounts:` has isolated data paths. Active account set via `--account` flag or `active_account` config.

## Commands

```bash
# Install
pip install -U -r requirements.txt
camoufox fetch

# Session setup (opens browser for manual login)
python main.py --setup
python main.py --setup --force-relogin
python main.py --validate-session

# Group scraping
python main.py --scrape-only
python main.py --scrape-only --scrape-force

# Posting
python main.py --run-once --dry-run      # Safe test
python main.py --run-once --live         # Live posting

# Multi-account
python main.py --account account-2 --run-once --dry-run
python main.py --add new-account-id

# Web UI control panel
python main.py --ui --host 127.0.0.1 --port 8080

# Validation (no formal tests exist)
python -m compileall main.py core
```

## Code Conventions

**Type hints:** All functions use Python type hints. Use `from __future__ import annotations` at top of modules.

**Config access:** Always go through `core.config_loader.load_config()` which handles account merging. Never read `config.yaml` directly.

**Camoufox browser:** Use `camoufox_kwargs()` from `session_manager` to build browser launch parameters. Browser options come from `config.yaml > browser:` and `camoufox:` sections.

**Template format** (`templates/post_*.yaml`):
```yaml
text: |
  Post content here...
image: templates/images/optional.jpg
tags: [for, logging, only]
```

**Rotation modes:** `single` (first template only), `sequential` (cycle templates per session), `round-robin` (cycle per group), `random`.

## Key Constraints

- **System Python only:** No virtual environments. Use system Python directly.
- **Config-driven behavior:** All scheduler/browser options must come from `config.yaml`. No hardcoding runtime overrides.
- **No scroll after composer:** During posting flow, do not add background scroll/motion after the composer popup opens (causes Facebook detection).
- **Preserve group metadata:** When merging scraped groups, keep existing `active` status and user data.
- **Session files are local:** Never commit `data/` contents. Sessions are machine-specific.

## PRD Reference

See `fb-autoposter-prd.md` for full product spec including:
- Anti-detection strategy (human-like delays, volume limits)
- Error handling patterns
- Recommended delay configurations
