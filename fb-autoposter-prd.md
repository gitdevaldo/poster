# PRODUCT REQUIREMENTS DOCUMENT

## Facebook Group Auto-Poster

Automated content distribution tool for digital product sellers.

- Version: v1.0
- Status: Draft
- Stack: Python + Camoufox
- Target: Digital sellers

# 1. Overview

Facebook Group Auto-Poster is a local automation tool built for digital product sellers who manually post promotional content across multiple Facebook groups. The tool uses Camoufox, a hardened Firefox-based browser with anti-detection patches, to automate the repetitive task of posting to each group on a scheduled rotation, significantly reducing time spent on manual distribution.

### Problem Statement

- Digital product sellers often join 50-200+ Facebook groups to reach buyers.
- Manual posting to each group takes 3-5 minutes per post x number of groups = hours of daily work.
- Existing tools (Postly, Publer) support Facebook Pages, but not Groups.
- Raw browser automation (Puppeteer, vanilla Playwright) gets flagged quickly by Facebook bot detection.

# 2. Goals and Non-Goals

## 2.1 Goals

- Automate posting to a list of joined Facebook groups on a configurable schedule.
- Manage session persistence so re-login is minimized.
- Scrape and maintain an up-to-date list of joined groups.
- Support multiple post templates with text and optional images.
- Mimic human-like behavior to minimize detection risk.
- Provide a simple CLI or minimal UI to manage queues and logs.

## 2.2 Non-Goals

- Not a SaaS product; runs locally on the user's machine.
- Not designed for posting to Pages (use native Meta tools for that).
- Not a general-purpose social media scheduler.
- No support for other platforms (Instagram, Twitter) in v1.

# 3. System Architecture

The system is composed of four main modules that operate in sequence.

```text
+---------------------------------------------------------+
| FB Group Auto-Poster                                    |
+--------------+--------------+-------------+-------------+
| Session Mgr  | Group Scrpr  | Post Queue  | Scheduler   |
| (auth.py)    | (scraper.py) | (queue.py)  | (main.py)   |
+--------------+--------------+-------------+-------------+
                \             |             /
                 +------------+------------+
                        Camoufox Browser
                     (anti-detection layer)
```

## 3.1 Module Breakdown

| Module | Responsibility |
| --- | --- |
| session_manager.py | Login once, persist session to JSON, detect expiry, re-login if needed |
| group_scraper.py | Navigate to /groups/feed, extract group names and URLs, save to groups.json |
| post_queue.py | Load post templates, manage posting order, track posted/pending status |
| scheduler.py | Main loop: iterate groups, trigger posts, enforce human-like delays |
| config.py | Load user config (intervals, blacklist, post rotation mode) |
| logger.py | Write structured logs per session: successes, failures, skips |

# 4. Project Folder Structure

```text
fb-autoposter/
|- main.py                      # Entry point
|- config.yaml                  # User configuration
|- requirements.txt
|
|- core/
|  |- session_manager.py        # Login, session save/load/refresh
|  |- group_scraper.py          # Extract joined groups from FB
|  |- post_queue.py             # Template loader + posting logic
|  |- scheduler.py              # Main posting loop + delay engine
|  |- logger.py                 # Structured log writer
|
|- data/
|  |- session.json              # Saved browser session state
|  |- groups.json               # Scraped group list (name + url)
|  |- posted_log.json           # History: what was posted, when, where
|  |- blacklist.txt             # Groups to skip
|
|- templates/
|  |- post_1.yaml               # Post template: text + image path
|  |- post_2.yaml
|  |- images/                   # Optional images for posts
|
|- logs/
   |- session_YYYYMMDD.log
```

# 5. Module Specifications

## 5.1 session_manager.py

Handles all authentication and session lifecycle.

### Responsibilities

- On first run: launch Camoufox, navigate to Facebook, wait for manual login, then save session to data/session.json.
- On subsequent runs: load session.json into browser context.
- After each posting session: re-save session.json to capture refreshed cookies.
- Detect session expiry by checking if current URL contains /login.
- If expired: prompt user or trigger re-login flow.

### Key Functions

```python
def load_or_create_session(browser) -> object:
    """Load session.json if exists, else do fresh login."""


def save_session(context, path="data/session.json") -> None:
    """Persist updated cookies after each run."""


def is_session_valid(page) -> bool:
    """Check for login redirect or auth wall."""


def do_manual_login(page) -> None:
    """Open login page, wait for user login, then save session."""
```

## 5.2 group_scraper.py

Extracts the list of Facebook groups the user has joined.

### Scraping Flow

- Navigate to facebook.com/groups/feed/.
- Scroll page to lazy-load all groups.
- Extract each group card: name + URL (/groups/{id}).
- Save to data/groups.json with timestamp.
- On subsequent runs: merge new groups, do not overwrite existing.

### groups.json Schema

```json
[
  {
    "id": "123456789",
    "name": "Jual Beli Digital Indonesia",
    "url": "https://www.facebook.com/groups/123456789",
    "last_posted": null,
    "active": true
  }
]
```

## 5.3 post_queue.py

Manages post templates and tracks posting state per group.

### Post Template Format (post_1.yaml)

```yaml
text: |
  Halo semua! 👋
  Saya jual VPS murah mulai 50rb/bulan.
  Spek: 1 vCPU, 1GB RAM, 20GB SSD.
  Minat? DM atau komen di bawah! 🔥
image: templates/images/vps-promo.jpg  # optional
tags: [vps, hosting, murah]            # for logging only
```

### Posting Rotation Modes

- sequential: post_1 to all groups, then post_2 to all groups, and so on.
- round-robin: post_1 to group A, post_2 to group B, post_3 to group C, then cycle.
- random: randomly pick a template per group each session.

### Posting Logic

```python
def post_to_group(page, group_url, template):
    page.goto(group_url)
    page.wait_for_timeout(random_delay(2000, 4000))

    # Scroll slightly to simulate reading.
    page.mouse.wheel(0, random.randint(200, 500))
    page.wait_for_timeout(random_delay(1000, 2000))

    # Click post box.
    page.click('[data-testid="group-composer-enter-composer"]')
    page.wait_for_timeout(random_delay(800, 1500))

    # Type content with human-like delay.
    human_type(page, template["text"])

    # Attach image if present.
    if template.get("image"):
        attach_image(page, template["image"])

    # Click Post button.
    page.click('[data-testid="react-composer-post-button"]')
    page.wait_for_timeout(random_delay(3000, 5000))
```

## 5.4 scheduler.py

Main loop. Iterates over the group list and triggers posts with human-like timing.

### Scheduler Flow

```python
for group in active_groups:
    if group_was_posted_less_than_cooldown_hours_ago(group):
        log_skip(group)
        continue

    template = queue.get_next_template(group)
    post_to_group(page, group.url, template)
    update_posted_log(group, template)

    wait_random_interval(min_delay, max_delay)

    # Every N groups: take a longer break.
    if posts_this_session % rest_every == 0:
        wait_long_break_interval()
```

### Recommended Delay Config

| Parameter | Recommended Value |
| --- | --- |
| min_delay between groups | 4 minutes |
| max_delay between groups | 9 minutes |
| long_break every N posts | Every 10 posts |
| long_break duration | 20-40 minutes |
| max_posts_per_session | 15 groups |
| cooldown per group | 24 hours |

# 6. Configuration File

All user-facing settings live in config.yaml. No code changes needed to adjust behavior.

```yaml
session:
  path: data/session.json
  auto_relogin: true

groups:
  source: data/groups.json
  blacklist: data/blacklist.txt
  rescrape_every_days: 7

posting:
  rotation_mode: round-robin   # sequential | round-robin | random
  min_delay_minutes: 4
  max_delay_minutes: 9
  max_posts_per_session: 15
  cooldown_hours: 24           # min time before reposting to same group
  rest_every_n_posts: 10
  rest_duration_minutes: 30

browser:
  headless: false              # visible browser recommended
  locale: id-ID
  timezone: Asia/Jakarta
```

# 7. Anti-Detection Strategy

### Why Camoufox

- Camoufox patches Firefox at the browser level (not JS injection).
- Spoofs: canvas fingerprint, WebGL, fonts, screen resolution, navigator properties.
- Harder to detect than puppeteer-extra-stealth JS patching.
- Behaves more like a genuine Firefox user.

## 7.1 Human Behavior Mimics

- Random keystroke delays when typing (30-120 ms per character).
- Random scroll before clicking the post box.
- Variable wait time after page load before interacting.
- Non-uniform delays between groups.
- Long breaks between posting batches.

## 7.2 Session Strategy

- Always use headless: false.
- Keep session warm by running daily.
- Save session.json after every run.
- Do not share session files across machines.

## 7.3 Volume Limits

- Max 15 groups per session.
- 24-hour cooldown per group before reposting.
- Do not run multiple sessions simultaneously.

# 8. Data Flow

```text
FIRST RUN
  main.py
    -> session_manager: no session.json found
      -> launch Camoufox, open Facebook login
      -> user logs in manually
      -> save session.json
    -> group_scraper: no groups.json found
      -> navigate to /groups/feed
      -> scroll + extract group cards
      -> save groups.json
    -> scheduler: begin posting loop
      -> load templates from /templates
      -> for each group: post -> log -> wait
      -> save updated session.json

SUBSEQUENT RUNS
  main.py
    -> session_manager: load session.json
      -> validate session (check login redirect)
    -> group_scraper: load groups.json (rescrape if stale)
    -> scheduler: resume posting loop
      -> skip groups posted < 24hr ago
      -> post remaining -> log -> wait
      -> save updated session.json
```

# 9. Error Handling

| Scenario | Handling |
| --- | --- |
| Session expired (login redirect) | Trigger re-login flow or alert user to login manually |
| Group not found / URL 404 | Skip group, mark as inactive in groups.json, log warning |
| Post box not found (FB UI change) | Log error with screenshot, skip group, continue |
| CAPTCHA detected | Pause automation, alert user, wait for manual resolution |
| Network timeout | Retry up to 2x with backoff, then skip group |
| Image upload fails | Post text only, log warning |
| Rate limit suspected | Immediately stop session, extend next run delay |

# 10. Development Milestones

| Phase | Deliverable |
| --- | --- |
| Phase 1 - Foundation | Camoufox setup, session_manager.py, manual login + save working |
| Phase 2 - Scraper | group_scraper.py extracts and saves joined groups to groups.json |
| Phase 3 - Posting Core | post_queue.py posts a single template to a single group end-to-end |
| Phase 4 - Scheduler | scheduler.py loops all groups with delays and logs results |
| Phase 5 - Config + Templates | config.yaml system, multi-template support, rotation modes |
| Phase 6 - Hardening | Error handling, CAPTCHA detection, session refresh, blacklist support |
| Phase 7 - Polish | CLI flags, dry-run mode, summary report after each session |

# 11. Dependencies

```txt
# requirements.txt
camoufox[geoip]    # Anti-detection Firefox browser
playwright          # Browser automation layer
pyyaml              # Config and template parsing
rich                # Terminal UI / pretty logs
schedule            # Optional: cron-like scheduling
Pillow              # Image processing for attachments
```

## Installation

```bash
pip install camoufox[geoip] playwright pyyaml rich Pillow
python -m camoufox fetch    # Download Firefox binaries
python main.py --setup      # First run: login + group scrape
```

# 12. Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Facebook UI changes break selectors | Use multiple fallback selectors per action; log failures with screenshots |
| Account flagged/restricted | Stay within volume limits, use realistic delays, never run headless |
| Session invalidated remotely by Facebook | Detect on next run, prompt re-login, do not rely on session forever |
| Group posting restrictions (admin-only) | Detect post-submit error, mark group as restricted in groups.json |
| IP flagged | Run from home IP, not VPS/datacenter |

_End of document._
