# Change Log

Log entries in reverse-chronological order (newest first).

---

## 2026-03-29 19:46

**Fix schedule ID persistence and clarify saved-record behavior**

Resolved unstable schedule ID behavior:

- Root cause: schedule records were normalized in memory but not always persisted immediately to `config.yaml`, causing IDs to appear to change across refreshes in some flows.
- Fix: schedule normalization/migration now writes back to config on read paths when needed.
  - Legacy `account.schedule` is migrated to `account.schedules`.
  - Normalized schedule fields (including stable `id`) are persisted.
- Result: schedule IDs remain stable once saved, and records are truly persisted in YAML.

Validation:
- `python3 -m compileall main.py core` passed.

**Files changed:**
- core/web_ui.py

---

## 2026-03-29 19:35

**Refactor scheduler to persisted multi-record queue + inline preset controls**

Addressed scheduler UX/data model issues and preset row alignment:

- Preset row layout changed to inline compact control:
  - dropdown is no longer full width
  - dropdown + action buttons render on one line
- Scheduler model changed from single in-memory slot to persisted records per account:
  - schedules saved under `accounts.<id>.schedules` (list)
  - supports multiple saved schedules per account
  - one-time/specific schedules are stored as fixed `run_at`
  - recurring schedules keep periodic rules
- Added scheduler records UI list:
  - shows id/type/next run/last run/mode/status
  - allows stopping individual schedules
  - Stop All disables all enabled schedules for selected account
- Added background scheduler loop that reads persisted schedules and executes due jobs.
- Migrates legacy single `account.schedule` to new list model automatically on read/save.

Validation:
- `python3 -m compileall main.py core` passed.

**Files changed:**
- core/web_ui.py

---

## 2026-03-29 19:20

**Shorten preset action buttons and add tooltips**

Refined Presets card button UX by removing long button text and using icon-only action buttons with hover tooltips.

- `Save New`, `Update`, `Delete` buttons are now compact icon-only (`💾`, `📝`, `🗑️`).
- Added `title` tooltips and `aria-label` accessibility labels for each button.
- Dynamic tooltip text now includes selected preset filename for update/delete actions.

Validation:
- `python3 -m compileall main.py core` passed.

**Files changed:**
- core/web_ui.py

---

## 2026-03-29 18:44

**Implement Web UI posting scheduler (one-time/daily/weekly/specific datetime)**

Completed scheduler feature in Web UI with per-account persistence and runtime control:

- Added scheduler modes:
  - `one_time` (next occurrence at HH:MM)
  - `daily` (HH:MM)
  - `weekly` (selected weekdays + HH:MM)
  - `specific_datetime` (single date/time run)
- Added timezone-aware normalization and next-run computation using `zoneinfo`.
- Persisted schedule under each account in `config.yaml` (`accounts.<id>.schedule`).
- Added scheduler runtime state in Web UI backend:
  - start/stop schedule actions
  - waiting/executing/stopping/completed/error states
  - next/last run metadata and last result
- Added Scheduler UI controls and wiring:
  - type/time/timezone/day selection/specific datetime
  - run mode (live/dry run)
  - Start Schedule / Stop Schedule buttons
  - status line with next/last run and account context
- Fixed scheduler-runner lock safety by using re-entrant locking and avoiding deadlock paths.
- Added saved-schedule prefill behavior so schedule settings are restored per account.

Validation:
- `python3 -m compileall main.py core` passed.

**Files changed:**
- core/web_ui.py

---

## 2026-03-29 18:18

**Polish preset UX text/buttons and replace native preset dialogs**

Improved preset UX consistency and clarity:

- Reworded unsaved dialog buttons:
  - `Save to Preset` → `Save as Preset`
  - `Save to Config` → `Continue (Config only)`
  - `Discard` → `Cancel`
- Added dedicated styled modal for preset actions (apply/disable/delete)
  - Removed native window confirm for preset delete/apply flows
- Preset selector now asks confirmation before apply/disable
- Update/Delete button labels now show selected preset filename
  - Example: `📝 Update (marketing-post.yaml)`
- Clarified unsaved dialog behavior text and kept state logic predictable

**Files changed:**
- core/web_ui.py

---

## 2026-03-29 18:13

**Add full preset system (save/apply/update/delete) with sidebar UI**

Implemented full-config preset feature with YAML storage in `templates/presets/` and Web UI controls at the top of the left sidebar.

What was added:
- New `core/preset_manager.py` for preset CRUD and apply logic
- Preset-aware config loading in `core/config_loader.py`
- New `presets` section in `config.yaml` (`enabled`, `name`)
- Sidebar Presets card (above Accounts) with:
  - Preset dropdown
  - Save New / Update / Delete buttons
  - Active preset info
  - Unsaved changes badge
- Modal dialogs (not native window dialogs):
  - Save Preset modal
  - Unsaved Changes modal with actions:
    - Save to Preset
    - Save to Config
    - Discard
- Preset actions wired through `/api/action`:
  - `save_preset`, `update_preset`, `delete_preset`, `apply_preset`, `disable_preset`
- Preset now includes account + browser/groups/posting rules + group include/exclude state (`groups_state`)

Behavior details:
- Selecting a preset applies it and enables `presets.enabled: true`.
- Selecting “None (Use Config)” disables preset mode.
- Saving/updating a preset also applies it immediately.
- Unsaved dialog appears before running setup/scrape/run actions when changes exist.

Validation:
- `python3 -m compileall main.py core` passed.
- Preset manager smoke test passed (save/list/apply/disable flow).

**Files changed:**
- core/preset_manager.py (created)
- core/config_loader.py
- core/web_ui.py
- config.yaml
- templates/presets/.gitkeep

## 2026-03-28 13:28

**Add posting progress indicator (Post X/X) to header**

Added a dynamic progress indicator in the top right header that shows current posting progress:
- Displays "📮 Post 5/10" format when posting is running
- Automatically hides when posting completes or no posting is active
- Parses "Processing group X/Y" messages from live logs

Backend: Added progress logging in scheduler.py for each group being processed.

**Files changed:**
- core/web_ui.py (progress badge + JS parsing logic)
- core/scheduler.py (added progress log event in posting loop)

---

## 2026-03-28 13:23

**Remove Global Settings summary info from sidebar**

Removed the info text lines from Global Settings section:
- "Locale id-ID | TZ Asia/Jakarta | Headless true | Humanize false"
- "Rescrape 7d | IdleStop 4"
- "Template ... | Delay ... | Skip ..."

Kept only the three buttons (Browser Rules, Groups Rules, Posting Rules).
Also removed the unused JS render functions.

**Files changed:**
- core/web_ui.py

---

## 2026-03-28 12:58

**Add Dual-Mode Login (Visual/Headless) to PRD and Implementation Plan**

Added comprehensive documentation for the automated login feature:

PRD Section 6.2 - Facebook Login (Dual Mode):
- Mode A: Visual Browser (Windows/Desktop) - browser window visible for manual login
- Mode B: Headless Automation (Linux/VPS) - credentials entered in Web UI, OTP handling
- Login detection table (OTP, security checkpoint, errors)
- Security considerations (no credential storage)

Implementation Plan Phase 5 - Automated Login:
- auto_login.py module with login/OTP functions
- Login flow detection (success, OTP, errors)
- Web UI login form for headless mode
- Backend API endpoints (/api/fb-login, /api/fb-submit-otp)
- Mode detection based on config
- Security measures and error handling

Updated timeline (now 9 phases, ~3-4 weeks) and file list.

**Files changed:**
- docs/license-system-prd.md
- docs/plans/license-system-implementation.md

---

## 2026-03-28 12:52

**Add CI/CD Build Pipeline to PRD and Implementation Plan**

Added comprehensive GitHub Actions build pipeline documentation:
- PRD Section 9: Build & Release Pipeline with architecture diagrams
- GitHub Actions workflow example for multi-platform builds (Linux, macOS, Windows)
- Version update flow (manual and auto-check options)
- Local build script template
- Release checklist
- Implementation Plan Phase 5: Build & Release Pipeline with detailed tasks
- Updated timeline to include new phase
- Updated file list with new workflow files

**Files changed:**
- docs/license-system-prd.md
- docs/plans/license-system-implementation.md

---

## 2026-03-28 11:42

**Create License System PRD and Implementation Plan**

Created comprehensive documentation for commercializing the auto-poster:
- Full PRD with business model, tiers, technical architecture, API specs, security measures, and database schema
- Implementation plan with 7 phases, timeline estimates, and MVP scope

**Files changed:**
- docs/license-system-prd.md (created)
- docs/plans/license-system-implementation.md (created)

---

## 2026-03-28 11:24

**Replace native confirm with styled modal for Run Live**

Replace browser's native `confirm()` dialog with a styled modal matching the app's UI:
- "🔄 Reset & Post All" button - clears history, posts to all groups
- "▶️ Post New Only" button - keeps history, posts to non-posted only
- Can close by clicking X or outside the modal

**Files changed:**
- core/web_ui.py

---

## 2026-03-28 11:18

**Add reset posted history dialog on Run Live**

When clicking "Run Live" button, show a confirm dialog asking whether to reset posted history:
- YES: Clear posted_log.json and post to ALL groups
- NO: Keep history and post to non-posted groups only

Added `clear_posted_log` backend action to delete the posted log file.

**Files changed:**
- core/web_ui.py

---

## 2026-03-28 11:06

**Fix template feature issues 3-6**

- Issue 3: Clear editor form state when modal closes to prevent stale data persisting
- Issue 4: Add delete template functionality with confirmation dialog
- Issue 5: Show ★ indicator for active template in dropdown
- Issue 6: Auto-set newly created template as active for the account

**Files changed:**
- core/web_ui.py

---

## 2026-03-28 11:00

**Fix template dropdown resetting to active template**

Preserve user's current template selection when state reloads, instead of always resetting to the account's active template. Only fall back to active template if current selection is no longer valid.

**Files changed:**
- core/web_ui.py

---

## 2026-03-28 10:55

**Remove Quick Actions label from toolbar**

Removed the "Quick Actions" label from the toolbar card in the right column.

**Files changed:**
- core/web_ui.py

---

## 2026-03-28 10:54

**Move active badge to card header top-right**

Position the active account badge in the card title row aligned right, instead of below the stats section above the select dropdown.

**Files changed:**
- core/web_ui.py

---

## 2026-03-28 10:51

**Fix template editor auto-filling bug**

Remove fillTemplateEditor() calls from updateTemplatePreview() to prevent the add/edit form from being overwritten when template dropdown changes, state reloads, or template modal opens.

**Files changed:**
- core/web_ui.py

---

## 2026-03-28 10:41

**Add agent behaviour rules and lessons**

Added context recovery, git commit scope, planning rules, change log rules, verification requirements, no lazy fixes, simplicity & minimal impact principles, self-improvement loop, workflow orchestration, and 7 mandatory principles from past lessons.

**Files changed:**
- .github/copilot-instructions.md
- .claude/CLAUDE.md (created)
