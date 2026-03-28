# License System Implementation Plan

## Overview
Implementation plan for the License System as defined in `docs/license-system-prd.md`.

---

## Phase 1: Client-Side License Module
**Goal:** Add license validation to the poster application.

### 1.1 Create license module
- [ ] Create `core/license.py` with:
  - `get_machine_id()` — Generate unique machine fingerprint
  - `validate_license(key)` — Call API to validate
  - `activate_license(key)` — First-time activation
  - `deactivate_license()` — Release machine binding
  - `get_cached_license()` — Read from local cache
  - `cache_license(data)` — Save validation result locally
  - `is_license_valid()` — Main check (cache + API fallback)

### 1.2 Add license config schema
- [ ] Update `config.yaml` schema to include:
  ```yaml
  license:
    key: ""
    tier: ""
    max_accounts: 1
    expires_at: null
    cached_at: null
    machine_id: ""
  ```

### 1.3 Integrate license check into startup
- [ ] Modify `main.py` to:
  - Check license on startup (before any action)
  - Prompt for key if not configured
  - Display license status
  - Exit gracefully if invalid

### 1.4 Add license CLI commands
- [ ] `--activate <key>` — Activate a new license
- [ ] `--deactivate` — Deactivate current machine
- [ ] `--license-status` — Show current license info

### 1.5 Enforce tier limits
- [ ] Check `max_accounts` before adding accounts
- [ ] Show warning when approaching limit
- [ ] Block account creation when limit reached

---

## Phase 2: License Server API
**Goal:** Build the backend API for license validation.

### 2.1 Choose hosting
- [ ] Option A: Serverless (Cloudflare Workers / Vercel Edge)
- [ ] Option B: Simple VPS (Node.js / Python FastAPI)
- [ ] Option C: Supabase (PostgreSQL + Edge Functions)

**Recommended:** Supabase for simplicity (DB + API in one).

### 2.2 Database setup
- [ ] Create `licenses` table
- [ ] Create `activations` table
- [ ] Create `validation_log` table
- [ ] Add indexes for performance

### 2.3 Implement API endpoints
- [ ] `POST /api/v1/activate`
  - Validate key format
  - Check key exists in DB
  - Check activation limit
  - Bind machine ID
  - Return license details

- [ ] `POST /api/v1/validate`
  - Validate key + machine_id
  - Check expiration
  - Log validation attempt
  - Return license status

- [ ] `POST /api/v1/deactivate`
  - Verify key + machine_id match
  - Remove activation record
  - Return remaining activations

### 2.4 API security
- [ ] Rate limiting (10 req/min per IP)
- [ ] Request signing (HMAC)
- [ ] HTTPS only
- [ ] Input validation

---

## Phase 3: Payment Integration
**Goal:** Automate license generation on purchase.

### 3.1 Choose payment platform
- [ ] Option A: LemonSqueezy (recommended for digital products)
- [ ] Option B: Gumroad
- [ ] Option C: Paddle

### 3.2 Create product listings
- [ ] Starter tier — $29
- [ ] Pro tier — $79
- [ ] Agency tier — $149
- [ ] Monthly subscription — $19/mo

### 3.3 Webhook integration
- [ ] On `order.completed`:
  - Generate unique license key
  - Create license record in DB
  - Email key to customer

- [ ] On `order.refunded`:
  - Revoke license key
  - Email notification

- [ ] On `subscription.renewed`:
  - Extend expiration date

- [ ] On `subscription.cancelled`:
  - Set expiration to end of billing period

### 3.4 License key generation
- [ ] Format: `POSTER-{TIER}{RANDOM}-{RANDOM}-{RANDOM}-{CHECK}`
- [ ] Checksum for validation
- [ ] Store hashed in DB

---

## Phase 4: Web UI Integration
**Goal:** Show license status in the dashboard.

### 4.1 Add license status card
- [ ] Show in sidebar or header:
  - Current tier badge
  - Accounts: X / Y used
  - Expiration (if applicable)
  - Machine ID (for support)

### 4.2 Add license management modal
- [ ] View license details
- [ ] Deactivate button (with confirmation)
- [ ] Link to purchase/upgrade

### 4.3 Enforce limits in UI
- [ ] Disable "Add Account" when limit reached
- [ ] Show upgrade prompt

---

## Phase 5: Automated Login (Dual Mode)
**Goal:** Support both visual browser (Windows) and headless automation (Linux/VPS) login modes.

### 5.1 Create auto-login module
- [ ] Create `core/auto_login.py` with:
  - `automated_login(email, password)` — Main login function
  - `submit_otp(code)` — Submit OTP/2FA code
  - `detect_login_state()` — Check current page state
  - `get_login_error()` — Extract error message if failed

### 5.2 Implement login flow detection
- [ ] Detect login page elements (email input, password input)
- [ ] Detect OTP/2FA prompt (SMS, Authenticator)
- [ ] Detect security checkpoint ("Is this you?")
- [ ] Detect success (logged in, news feed visible)
- [ ] Detect errors (wrong password, account locked, CAPTCHA)

### 5.3 Web UI login form (headless mode)
- [ ] Add login form to Account Setup section:
  - Email input field
  - Password input field
  - Login button
  - Status indicator
- [ ] Add OTP input form:
  - Code input field
  - Submit button
  - Cancel button
- [ ] WebSocket/polling for real-time status updates

### 5.4 Backend API endpoints
- [ ] `POST /api/fb-login` — Start automated login
  ```json
  {"email": "...", "password": "..."}
  ```
  Returns: `{"status": "success"}` or `{"status": "otp_required", "type": "sms"}`

- [ ] `POST /api/fb-submit-otp` — Submit OTP code
  ```json
  {"code": "123456"}
  ```
  Returns: `{"status": "success"}` or `{"status": "error", "message": "..."}`

- [ ] `GET /api/fb-login-status` — Check login progress
  Returns: `{"state": "entering_password", "progress": 50}`

### 5.5 Mode detection and switching
- [ ] Auto-detect headless mode from config (`browser.headless`)
- [ ] Windows default: `headless: false` (visual browser)
- [ ] Linux default: `headless: true` (automated login)
- [ ] UI adapts based on mode:
  - Headless → Show login form
  - Visual → Show "Open Browser" button

### 5.6 Security measures
- [ ] Never store password (use only during login)
- [ ] Never log credentials
- [ ] Clear password from memory after use
- [ ] Credentials sent over localhost only

### 5.7 Error handling
- [ ] Wrong password → Show error, allow retry
- [ ] Account locked → Show message, suggest manual
- [ ] CAPTCHA → Show message, suggest manual or visual mode
- [ ] Network error → Retry with backoff
- [ ] Timeout → Cancel and show error

---

## Phase 6: Build & Release Pipeline
**Goal:** Automate binary builds and releases via GitHub Actions.

### 6.1 Create GitHub Actions workflow
- [ ] Create `.github/workflows/build-release.yml`
- [ ] Configure matrix build for Linux, macOS, Windows
- [ ] Set Python 3.11 as build environment
- [ ] Install Nuitka and dependencies

### 6.2 Configure Nuitka build
- [ ] Standalone mode (no Python required)
- [ ] Onefile output (single executable)
- [ ] Include `templates/` directory
- [ ] Include `config.example.yaml`
- [ ] Platform-specific options:
  - Windows: `--windows-icon-from-ico`
  - macOS: `--macos-create-app-bundle`
  - Linux: default

### 6.3 Artifact management
- [ ] Upload build artifacts from each platform
- [ ] Generate SHA256 checksums
- [ ] Attach to GitHub Release
- [ ] Auto-generate release notes from commits

### 6.4 Create local build script
- [ ] `scripts/build.sh` for local testing
- [ ] Version extraction from git tags
- [ ] Platform detection
- [ ] Clean build directory

### 6.5 Version checking (optional)
- [ ] Add `/api/v1/version` endpoint to license server
- [ ] Client checks on startup
- [ ] Display "Update available" notification
- [ ] Link to download portal

---

## Phase 7: Code Protection
**Goal:** Protect the distributed binary from reverse engineering.

### 7.1 Setup Nuitka compilation
- [ ] Install Nuitka: `pip install nuitka`
- [ ] Create build script:
  ```bash
  nuitka --standalone --onefile \
    --enable-plugin=anti-bloat \
    --include-data-dir=templates=templates \
    --output-filename=poster \
    main.py
  ```
- [ ] Test on Windows + Linux

### 7.2 Add PyArmor obfuscation (optional)
- [ ] Install PyArmor: `pip install pyarmor`
- [ ] Obfuscate before compilation
- [ ] Test license checks still work

### 7.3 Add runtime integrity checks
- [ ] Hash critical files on build
- [ ] Verify hashes at runtime
- [ ] Scatter license checks throughout code

### 7.4 Anti-debugging measures
- [ ] Detect common debuggers
- [ ] Exit silently if detected

---

## Phase 8: Admin Dashboard
**Goal:** Manage licenses from a web interface.

### 8.1 Basic CRUD
- [ ] List all licenses (with search/filter)
- [ ] View license details + activations
- [ ] Revoke/suspend license
- [ ] Extend expiration

### 8.2 Support tools
- [ ] Lookup by key or email
- [ ] Reset activations (for machine transfers)
- [ ] Add manual notes

### 8.3 Analytics
- [ ] Active licenses count
- [ ] New activations (daily/weekly)
- [ ] Validation success/failure rate

---

## Phase 9: Documentation & Launch
**Goal:** Prepare for public release.

### 9.1 User documentation
- [ ] Installation guide
- [ ] License activation guide
- [ ] Troubleshooting (common license errors)
- [ ] FAQ

### 9.2 Legal
- [ ] End User License Agreement (EULA)
- [ ] Refund policy
- [ ] Terms of Service

### 9.3 Marketing site
- [ ] Landing page
- [ ] Feature comparison table
- [ ] Testimonials (after beta)
- [ ] Purchase buttons

### 9.4 Launch checklist
- [ ] Beta test with 5-10 users
- [ ] Load test license server
- [ ] Setup monitoring/alerts
- [ ] Prepare support channels

---

## Timeline Estimate

| Phase | Duration | Dependencies |
|-------|----------|--------------|
| Phase 1: Client module | 2-3 days | None |
| Phase 2: License server | 2-3 days | Phase 1 |
| Phase 3: Payment integration | 1-2 days | Phase 2 |
| Phase 4: Web UI | 1 day | Phase 1 |
| Phase 5: Automated login | 2-3 days | Phase 4 |
| Phase 6: Build pipeline | 1-2 days | Phase 1 |
| Phase 7: Code protection | 1-2 days | Phase 6 |
| Phase 8: Admin dashboard | 2-3 days | Phase 2 |
| Phase 9: Docs & launch | 2-3 days | All |

**Total:** ~3-4 weeks for MVP

---

## MVP Scope (Minimum for Launch)

**Must have:**
- [ ] Phase 1: Client license module
- [ ] Phase 2: License server API (basic)
- [ ] Phase 3: Payment integration
- [ ] Phase 5: Automated login (headless mode)
- [ ] Phase 6: Build pipeline (GitHub Actions)
- [ ] Phase 7: Code protection (Nuitka only)
- [ ] Phase 9: Basic docs + EULA

**Nice to have (post-launch):**
- [ ] Phase 4: Web UI license integration
- [ ] Phase 8: Admin dashboard
- [ ] PyArmor obfuscation
- [ ] Anti-debugging

---

## Files to Create/Modify

### New Files
| File | Purpose |
|------|---------|
| `core/license.py` | License validation logic |
| `core/auto_login.py` | Automated Facebook login (headless) |
| `license-server/` | Separate repo for API |
| `.github/workflows/build-release.yml` | CI/CD build pipeline |
| `scripts/build.sh` | Local build script |
| `docs/installation.md` | User setup guide |
| `docs/LICENSE.txt` | EULA |

### Modified Files
| File | Changes |
|------|---------|
| `main.py` | Add license check on startup |
| `config.yaml` | Add license section, headless default |
| `core/web_ui.py` | Add license card, login form (headless mode) |
| `core/session_manager.py` | Integrate auto_login for headless |
| `core/account_manager.py` | Enforce account limits |
| `requirements.txt` | Add requests (for API calls) |

---

## Review Checklist

Before marking complete:
- [ ] License validation works offline (grace period)
- [ ] Machine binding prevents key sharing
- [ ] Tier limits enforced correctly
- [ ] Compiled binary runs without Python installed
- [ ] Payment webhook creates valid licenses
- [ ] Refund webhook revokes licenses
- [ ] Headless login works on Linux VPS
- [ ] Visual browser login works on Windows
- [ ] OTP flow works end-to-end
- [ ] Error messages are user-friendly
- [ ] Documentation covers all scenarios
