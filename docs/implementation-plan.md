# Feasibility-Adjusted Implementation Plan

This plan converts the PRD into staged execution with explicit go/no-go criteria.

## Guardrails (Apply to Every Phase)

- No guarantee of long-term stability due to platform UI and policy changes.
- Keep run volume low until stability metrics are met.
- Stop immediately on CAPTCHA/rate-limit/account-warning signals.
- Every release must include selector fallback updates and regression check.

## Phase 0: Environment and Bootstrap

Scope:
- Setup Python env, dependencies, and Camoufox binaries.
- Verify Camoufox CLI and a single browser open/close cycle.

Acceptance criteria:
- `pip install -r requirements.txt` succeeds.
- `camoufox fetch` succeeds.
- Basic startup script exits cleanly.

Go/No-Go:
- Go only if startup is stable for 3 consecutive runs.

## Phase 1: Session Management

Scope:
- Manual login flow and persistent session storage.
- Session validity check and re-login trigger.

Acceptance criteria:
- Session file created and reused across runs.
- Login redirect detection works.
- Session refresh write-back after run.

Go/No-Go:
- Go only if session restore success >= 90% over 10 test runs.

## Phase 2: Group Inventory

Scope:
- Scrape joined groups list and persist to `data/groups.json`.
- Merge updates without losing existing metadata.

Acceptance criteria:
- Scrape job completes without crash.
- Known test groups detected consistently.
- `active` and metadata fields remain valid JSON schema.

Go/No-Go:
- Go only if extraction consistency >= 95% across 5 repeated runs.

## Phase 3: Single-Group Posting Core

Scope:
- Post one text template to one controlled test group.
- Optional image attach and post-submit confirmation check.

Acceptance criteria:
- Successful submit detection for text-only and text+image.
- Failure path logs screenshot and reason.

Go/No-Go:
- Go only if success >= 80% on controlled test set with no account warning.

## Phase 4: Queue and Rotation

Scope:
- Implement sequential, round-robin, and random template selection.
- Track per-group cooldown in posted log.

Acceptance criteria:
- Correct template selection by mode.
- Cooldown skip behavior works and is logged.

Go/No-Go:
- Go only if posting history remains internally consistent after 100 simulated selections.

## Phase 5: Scheduler and Pacing

Scope:
- End-to-end group loop with randomized delays and periodic long breaks.
- Max-posts-per-session enforcement.

Acceptance criteria:
- Timing policy is respected for all iterations.
- Run summary generated after each cycle.

Go/No-Go:
- Go only if 7-day canary run shows no severe platform warnings.

## Phase 6: Hardening and Recovery

Scope:
- Selector fallback strategy.
- Retry with backoff for transient failures.
- CAPTCHA/rate-limit immediate stop policy.

Acceptance criteria:
- Known failure scenarios handled without crash.
- Artifacts captured: logs + screenshots + event reason.

Go/No-Go:
- Go only if severe-failure recovery success >= 90% in test replay scenarios.

## Phase 7: Operational Readiness

Scope:
- CLI modes: `--setup`, `--run-once`, dry-run, summary report.
- Operational docs and maintenance checklist.

Acceptance criteria:
- One-command onboarding from clean machine.
- Maintainer can rotate selectors and run smoke test in < 30 minutes.

Go/No-Go:
- Go only if runbook is validated by someone other than original implementer.

## Ongoing Maintenance Loop

- Weekly selector health check.
- Monthly dependency and Camoufox update review.
- Canary group set for any change rollout.
- Track metrics:
  - Session restore rate
  - Post success rate
  - Skip/error/captcha counts
  - Mean time to fix selector breaks
