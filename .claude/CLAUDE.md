# Project Guidelines

Facebook Group Auto-Poster: multi-account automation for posting to Facebook groups using Camoufox (anti-detection Firefox).

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

---

## Agent Behaviour

### Context Recovery After Compaction (MANDATORY)

When context is compacted, recover full context BEFORE continuing:

1. Check auto-memory at `~/.claude/projects/c--Users-Administrator-Desktop-indexnow-dev/memory/`
2. Use claude-mem MCP tools (`search`, `timeline`, `get_observations`) if available
3. Read JSONL transcript at `~/.claude/projects/*/[session-id].jsonl` for specific details
4. NEVER rely solely on compacted summary. Do NOT ask the user to repeat information discussed earlier.

### Git Commit Scope Rules

- Commit and push after every change, including small changes.
- **Before every push**, run `git status` and `git diff` to check for user changes in the working tree.
- Stage files explicitly; do not use broad staging that can include secrets or generated files.
- Exclude `__pycache__/`, `*.pyc`, `node_modules/`, `.next/`, `dist/` unless explicitly asked.
- Use Conventional Commits with proper type, scope, and description.

### Planning Rule

For non-trivial tasks (3+ steps or architectural decisions), write a comprehensive markdown plan with step-by-step tasks, goals, and acceptance criteria. Store plans in `docs/plans/`. Do not execute code until explicitly told to implement.

### Change Log Rules

For every implementation/change, update `docs/log/log-changes.md` before commit/push. Each log entry must include:

- `Date time`
- `Short description`
- `What you do`
- `File path that changes`

Log entries appended in reverse-chronological order (newest first).

### Verification Before Done

Never mark a task complete without proving it works. Run `python -m compileall main.py core`, check for errors, demonstrate correctness.

### No Lazy Fixes

- Always find and fix root causes. Never apply temporary workarounds or band-aids.
- When fixing a file, check all other files that import from or depend on the changed code. Trace the full impact.
- Senior developer standards: would a staff engineer approve this change?

### Simplicity & Minimal Impact

- Make every change as simple as possible. Minimal code impact.
- Changes touch only what's necessary. Avoid introducing bugs.
- When changing shared code (`core/`), verify all consumer modules still work.

### Self-Improvement Loop

After ANY correction from the user, save the lesson to auto-memory (`~/.claude/projects/.../memory/`). Review memory at session start. Never repeat the same mistake.

### Workflow Orchestration

- **Plan Mode**: Enter plan mode for any non-trivial task. If something goes sideways, STOP and re-plan immediately.
- **Subagent Strategy**: Use subagents for research, exploration, and parallel analysis. One task per subagent. Keep main context window clean.
- **Autonomous Bug Fixing**: When given a bug report, just fix it. Point at logs/errors/failing tests, then resolve. Zero hand-holding required.
- **Demand Elegance (Balanced)**: For non-trivial changes, pause and consider if there's a more elegant approach. Skip for simple, obvious fixes — don't over-engineer.

### Generated Documents

All generated markdown documents (reports, PRDs, audits, plans) go in the `docs/` folder with descriptive filenames (date prefix when relevant).

---

## Lessons & Principles (Mandatory)

These are hard-won rules from past mistakes. Each is a PRINCIPLE to follow — violating any is a blocking issue.

### Principle 1: Verify Actual Call Chains — Don't Trust Code Is Wired Up

Before modifying or relying on any module, VERIFY it has actual callers. Grep for imports and function calls across all files. Zero callers = dead code. Flag it.

### Principle 2: Never Destroy Git History

Never create root commits on repos with existing history. Never force-push without verifying local history includes all remote commits. Before ANY push: `git fetch origin && git log origin/main --oneline -5`. Default to `git pull --rebase origin main`.

### Principle 3: Dead Code Cleanup Must Be Complete

When removing a feature, remove ALL traces — types, imports, config references, route files. No orphaned code.

### Principle 4: Never Defer Implementation Tasks — Only Defer Live Testing

Complete ALL implementation tasks. Never defer code changes to "a separate cleanup PR" or "later." The only deferrable tasks are those requiring a running deployment (live/E2E testing).

### Principle 5: Commit + Push Is ONE Atomic Action Chain

After committing, ALWAYS immediately push. Never stop after `git commit`. Full chain: `git commit` → `git push`.

### Principle 6: When Unsure, ASK — Never Guess and Change Code

If not 100% certain which element or issue the user means, ASK before making code changes. Never guess, assume, or make speculative changes. One wrong guess wastes time and erodes trust.

### Principle 7: Do Exactly What Is Asked — No Assumptions, No Extra Steps

Execute EXACTLY the user's instruction. Do not assume next steps, do not start additional work. If asked for information, give it and stop. If asked to delete something, delete ALL of it. Before taking action: "Did the user explicitly ask me to do this?" If no → don't do it.
