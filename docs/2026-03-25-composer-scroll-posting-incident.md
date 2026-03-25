# Incident Case: Composer Popup Background Scroll + Posting Failure

Date: 2026-03-25
Project: fbpost
Scope: Facebook group posting automation

## 1. Executive Summary

The automation repeatedly opens the group post composer popup but fails to complete posting reliably. During popup state, background page scrolling/movement is still observed. In multiple runs, the automation then skips/fails and moves to the next group, repeating the same behavior.

The issue is not a single selector typo. It is a flow control and interaction-scoping problem across composer detection, popup-only action handling, and failure handling after popup open.

## 2. User-Reported Symptoms

1. Initial trigger is clicked and popup appears.
2. After popup appears, background still scrolls/moves.
3. Post often is not submitted.
4. Automation proceeds to next group and repeats.
5. User provided explicit DOM samples for:
- pre-popup trigger area
- popup dialog structure
- popup textbox
- popup post button
- comment box structure

Core requirement stated repeatedly:
- Use strict 2-state flow.
- Once popup appears, do direct post only.
- Do not continue broad searching/hunting in background.
- Do not interact with comment boxes.

## 3. Exact Two-State Model Required

### State A: Pre-popup trigger

Only action:
1. Locate and click post trigger area on group page.

Exit condition:
1. Popup dialog appears.

### State B: Popup composer

Only actions (inside popup root):
1. Locate popup textbox.
2. Insert post content.
3. Optional image attach if available.
4. Click enabled Post button.

Hard rules:
1. No background page selector scanning.
2. No background scroll/movement activity.
3. No comment selector usage.
4. Fail current group if popup controls not found, do not hunt unrelated elements.

## 4. Evidence Collected

### 4.1 Runtime log behavior

From account posting logs:
1. Repeated failures with details such as:
- Image attachment failed
- Post button not found
2. Dry runs show logical pass because no real UI post action is required.

Interpretation:
1. Failures happen after navigation and composer phase.
2. Main observed blockers are popup action reliability (especially image flow and post button gating), not only trigger click.

### 4.2 DOM evidence (user provided)

User supplied full markup samples showing:
1. Trigger button before popup.
2. Popup root with role="dialog".
3. Popup textbox with contenteditable + role="textbox" + data-lexical-editor + aria-placeholder.
4. Post button with aria-label="Post" and disabled state toggling.
5. Comment area with aria-label/placeholder including "Comment as ...".

This confirms that popup interactions should be anchored to popup dialog root and aria attributes, not generic textboxes across page.

## 5. Changes Already Applied During Incident

## 5.1 Selector narrowing

1. Moved toward dialog-scoped textbox and post selectors.
2. Added comment avoidance guards during text fill.

## 5.2 Scroll/motion controls

1. Added background scroll lock/unlock helpers around popup compose/post stage.
2. Added click visibility checks to avoid blind click attempts.

## 5.3 Posting failure handling

1. Changed image attachment behavior to avoid hard abort when image UI is unavailable.
2. Continue text-only post path if image attachment fails.

## 5.4 Group active-state preservation

1. Strengthened merge logic in group scraper to preserve existing active flags by id/url mapping.

## 5.5 Instruction hardening

Project instruction updated to enforce:
1. Browser behavior remains config-driven.
2. No background motion activity after popup opens unless explicitly requested.

## 6. Why the Issue Persisted Despite Multiple Patches

1. Root cause spans more than one layer:
- selector strategy
- popup state transition logic
- retry behavior
- attachment fallback and post-button readiness

2. Some patches reduced risk but did not fully enforce a strict popup-root execution context.

3. Any remaining page-level fallback can still reintroduce background movement or irrelevant retries.

## 7. Root-Cause-Oriented Fix Plan (Definitive)

### 7.1 Popup-root handle

1. After trigger click, acquire single popup root locator:
- outer dialog: role="dialog" with aria-labelledby
- inner create-post dialog context

2. All subsequent locators must be queried from popup root only.

### 7.2 State transition contract

1. State A can only do trigger click attempts.
2. State B can only do popup actions.
3. No fallback to page-root selectors after State B entered.

### 7.3 Post submit gating

1. Wait until textbox has inserted content.
2. Wait until Post button becomes enabled (aria-disabled != true).
3. Click Post from popup root.

### 7.4 Attachment behavior

1. If image attach control missing/unavailable in popup, log warning and continue text-only post.
2. Do not fail group solely due to missing image UI.

### 7.5 Fail-fast boundaries

1. If popup-root textbox not found in bounded timeout, fail group with screenshot and reason.
2. Do not continue broad selector hunting.

### 7.6 Telemetry for verification

Add explicit structured logs:
1. state_enter: pre_popup
2. state_exit: pre_popup_success
3. state_enter: popup
4. popup_textbox_found
5. popup_post_button_enabled
6. popup_post_clicked
7. popup_flow_failed (with reason)

## 8. Acceptance Criteria

1. When popup appears, no visible background scroll/motion.
2. No comment box interactions.
3. Successful text-only post when images unavailable.
4. No repeated popup-open without submit attempt in same group cycle.
5. If failure occurs, reason is explicit and bounded (no selector thrashing).

## 9. Reproduction Checklist

1. Set run mode live for one group.
2. Use a template with text and images.
3. Observe:
- popup opens
- text inserted
- if image attach unavailable, warning log only
- post button enabled
- post submit click
4. Repeat on 3 groups with different UI variants.

## 10. Current Status

1. Multiple defensive improvements are already in place.
2. Incident not considered closed until strict popup-root-only execution is fully enforced and verified on live groups.

## 11. Next Action (Recommended)

Implement final structural change:
1. Refactor posting routine to use a popup locator object passed through all compose helpers.
2. Remove remaining page-level fallback in compose/post stage.
3. Validate with single-group live runs and capture event logs for each state transition.
