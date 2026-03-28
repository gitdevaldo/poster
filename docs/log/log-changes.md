# Change Log

Log entries in reverse-chronological order (newest first).

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
