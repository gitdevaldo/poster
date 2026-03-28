# License System PRD

## Product Requirements Document
**Product:** Facebook Group Auto-Poster License System  
**Version:** 1.0  
**Date:** 2026-03-28  
**Status:** Draft

---

## 1. Overview

### 1.1 Purpose
Transform the Facebook Group Auto-Poster from an open tool into a commercially distributable product with license-based access control, enabling monetization through direct sales.

### 1.2 Business Model
- **Distribution:** Self-hosted by customers (on their own VPS/PC)
- **Revenue:** License key sales (one-time or subscription)
- **Target Market:** Social media marketers, affiliate marketers, small agencies

### 1.3 Goals
1. Prevent unauthorized use of the software
2. Enable tiered pricing based on feature access
3. Bind licenses to specific machines to prevent sharing
4. Support both perpetual and time-limited licenses
5. Minimize support burden from license issues

---

## 2. License Tiers

### 2.1 Tier Structure

| Tier | Price | Accounts | Features | License Type |
|------|-------|----------|----------|--------------|
| **Starter** | $29 | 1 | Core posting, Web UI | Perpetual |
| **Pro** | $79 | 5 | + Priority support | Perpetual |
| **Agency** | $149 | Unlimited | + White-label ready | Perpetual |
| **Monthly** | $19/mo | 3 | All features | Subscription |

### 2.2 Feature Matrix

| Feature | Starter | Pro | Agency |
|---------|---------|-----|--------|
| Multi-account posting | 1 account | 5 accounts | Unlimited |
| Web UI dashboard | ✓ | ✓ | ✓ |
| Template management | ✓ | ✓ | ✓ |
| Group scraping | ✓ | ✓ | ✓ |
| Scheduled posting | ✓ | ✓ | ✓ |
| Auto-skip posted | ✓ | ✓ | ✓ |
| Machine transfers | 1 | 3 | Unlimited |
| Updates | 6 months | 1 year | Lifetime |
| Support | Community | Email | Priority |

---

## 3. Technical Architecture

### 3.1 Components

```
┌─────────────────────────────────────────────────────────────┐
│                     Customer's Machine                       │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌──────────────┐    ┌───────────────┐  │
│  │  main.py    │───▶│ license.py   │───▶│ License API   │  │
│  │  (startup)  │    │ (validation) │    │ (your server) │  │
│  └─────────────┘    └──────────────┘    └───────────────┘  │
│         │                  │                    │           │
│         ▼                  ▼                    ▼           │
│  ┌─────────────┐    ┌──────────────┐    ┌───────────────┐  │
│  │ config.yaml │    │ Machine ID   │    │ License DB    │  │
│  │ license_key │    │ Fingerprint  │    │ (PostgreSQL)  │  │
│  └─────────────┘    └──────────────┘    └───────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 License Key Format

```
POSTER-XXXXX-XXXXX-XXXXX-XXXXX
       │      │      │      │
       │      │      │      └── Checksum (4 chars)
       │      │      └── Random segment
       │      └── Tier code (encoded)
       └── Product prefix
```

Example: `POSTER-PRO5A-K8M2N-QW3RT-7YU1`

### 3.3 Machine Fingerprint

Generate unique machine ID from:
- Hostname
- MAC address (primary NIC)
- CPU identifier
- OS + username hash

```python
fingerprint = sha256(hostname + mac + cpu_id + os_user)[:32]
```

### 3.4 Validation Flow

```
┌──────────┐     ┌──────────────┐     ┌─────────────┐
│  Start   │────▶│ Load config  │────▶│ Key exists? │
└──────────┘     └──────────────┘     └──────┬──────┘
                                             │
                      ┌──────────────────────┴──────────────────────┐
                      ▼                                             ▼
               ┌─────────────┐                               ┌─────────────┐
               │ Prompt for  │                               │ Validate    │
               │ license key │                               │ with API    │
               └──────┬──────┘                               └──────┬──────┘
                      │                                             │
                      ▼                                             ▼
               ┌─────────────┐                               ┌─────────────┐
               │ Save to     │                               │ Cache local │
               │ config.yaml │                               │ (24h grace) │
               └──────┬──────┘                               └──────┬──────┘
                      │                                             │
                      └──────────────────────┬──────────────────────┘
                                             ▼
                                      ┌─────────────┐
                                      │ Valid?      │
                                      └──────┬──────┘
                                             │
                         ┌───────────────────┴───────────────────┐
                         ▼                                       ▼
                  ┌─────────────┐                          ┌─────────────┐
                  │ Run app     │                          │ Show error  │
                  │ normally    │                          │ + exit      │
                  └─────────────┘                          └─────────────┘
```

### 3.5 Offline Grace Period

- Cache successful validation for **24 hours**
- Allow offline usage within grace period
- Re-validate when internet available
- Hard fail after grace period expires

---

## 4. License Server API

### 4.1 Endpoints

#### POST /api/v1/activate
First-time activation of a license key.

**Request:**
```json
{
  "license_key": "POSTER-PRO5A-K8M2N-QW3RT-7YU1",
  "machine_id": "a1b2c3d4e5f6...",
  "machine_name": "user-vps-1",
  "app_version": "1.0.0"
}
```

**Response (success):**
```json
{
  "valid": true,
  "tier": "pro",
  "max_accounts": 5,
  "expires_at": null,
  "activations_used": 1,
  "activations_max": 3,
  "features": ["web_ui", "multi_account", "templates"]
}
```

**Response (error):**
```json
{
  "valid": false,
  "error": "activation_limit_reached",
  "message": "This license has reached maximum activations (3/3)"
}
```

#### POST /api/v1/validate
Periodic validation check (cached locally).

**Request:**
```json
{
  "license_key": "POSTER-PRO5A-K8M2N-QW3RT-7YU1",
  "machine_id": "a1b2c3d4e5f6..."
}
```

**Response:**
```json
{
  "valid": true,
  "tier": "pro",
  "max_accounts": 5,
  "expires_at": null
}
```

#### POST /api/v1/deactivate
Release activation from a machine (for transfers).

**Request:**
```json
{
  "license_key": "POSTER-PRO5A-K8M2N-QW3RT-7YU1",
  "machine_id": "a1b2c3d4e5f6..."
}
```

**Response:**
```json
{
  "success": true,
  "activations_used": 0,
  "activations_max": 3
}
```

### 4.2 Error Codes

| Code | Description |
|------|-------------|
| `invalid_key` | License key format invalid or not found |
| `expired` | License has expired |
| `activation_limit_reached` | Max machines already activated |
| `machine_mismatch` | Machine ID doesn't match activation |
| `revoked` | License manually revoked (refund, abuse) |
| `network_error` | Could not reach license server |

---

## 5. Security Measures

### 5.1 Code Protection Layers

| Layer | Tool | Purpose |
|-------|------|---------|
| 1. Compilation | Nuitka | Convert to native binary |
| 2. Obfuscation | PyArmor | Encrypt bytecode |
| 3. API Validation | Custom | Server-side checks |
| 4. Runtime Checks | Scattered | Anti-tampering |

### 5.2 Anti-Tampering Measures

1. **Integrity checks** — Hash critical files at runtime
2. **Scattered validation** — Call license check in multiple places, not just startup
3. **Time-based checks** — Re-validate periodically during runtime
4. **Debug detection** — Detect debuggers and exit
5. **API response signing** — Sign server responses to prevent MITM

### 5.3 Machine Binding

- License key binds to first machine on activation
- Changing machines requires deactivation first
- Tier determines max activations (transfers)
- Suspicious patterns trigger manual review

---

## 6. User Experience

### 6.1 First-Time Setup

```
$ python main.py --setup

╔═══════════════════════════════════════════════════════════╗
║           Facebook Group Auto-Poster v1.0.0               ║
╠═══════════════════════════════════════════════════════════╣
║  License key required to continue.                        ║
║                                                           ║
║  Enter your license key:                                  ║
║  > POSTER-PRO5A-K8M2N-QW3RT-7YU1                         ║
║                                                           ║
║  ✓ License activated successfully!                        ║
║  • Tier: Pro                                              ║
║  • Accounts: 5 max                                        ║
║  • Expires: Never                                         ║
╚═══════════════════════════════════════════════════════════╝
```

### 6.2 Web UI License Status

Add license status card to dashboard showing:
- Current tier
- Accounts used / max
- Expiration date (if subscription)
- Machine ID (for support)
- Deactivate button

### 6.3 Error Messages

| Scenario | Message |
|----------|---------|
| No license | "License required. Purchase at yoursite.com" |
| Invalid key | "Invalid license key. Check for typos." |
| Expired | "License expired. Renew at yoursite.com" |
| Max activations | "Activation limit reached. Deactivate another machine first." |
| Offline (grace) | "Offline mode. Will re-validate when online." |
| Offline (expired) | "Cannot verify license. Connect to internet." |

---

## 7. Admin Dashboard (Your Side)

### 7.1 Features Needed

1. **License Management**
   - Generate new keys
   - View all licenses
   - Revoke/suspend keys
   - Extend expiration

2. **Activation Tracking**
   - See all activated machines
   - Remote deactivation
   - Activation history

3. **Analytics**
   - Active users
   - Revenue tracking
   - Conversion rates

4. **Support Tools**
   - Lookup by key/email
   - Reset activations
   - Issue refunds

### 7.2 Integration with Payment Platforms

**LemonSqueezy / Gumroad webhooks:**
- On purchase → Generate license key → Email to customer
- On refund → Revoke license key
- On subscription renewal → Extend expiration

---

## 8. Distribution Package

### 8.1 Deliverables

```
fb-autoposter-v1.0.0/
├── poster.exe           # Compiled binary (Windows)
├── poster               # Compiled binary (Linux)
├── config.example.yaml  # Example config
├── templates/           # Default templates
├── README.md            # Setup instructions
└── LICENSE.txt          # EULA
```

### 8.2 Installation Guide

1. Download and extract
2. Copy `config.example.yaml` to `config.yaml`
3. Run `./poster --setup`
4. Enter license key when prompted
5. Complete Facebook login in browser
6. Start posting

---

## 9. Success Metrics

| Metric | Target |
|--------|--------|
| Conversion rate | > 3% |
| Piracy rate | < 10% |
| Support tickets (license issues) | < 5% of sales |
| Churn (subscriptions) | < 8% monthly |
| Refund rate | < 5% |

---

## 10. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Key sharing | Revenue loss | Machine binding + activation limits |
| Cracking | Revenue loss | Multi-layer protection + frequent updates |
| Server downtime | Users locked out | 24h grace period + status page |
| FB blocks tool | Refund requests | Clear ToS + no guarantees |

---

## 11. Future Enhancements

1. **Hardware dongles** — USB-based licensing for enterprise
2. **Team licenses** — Multiple users under one org
3. **White-label** — Remove branding for agencies
4. **API access** — Programmatic control for power users
5. **Mobile companion** — Monitor posting from phone

---

## Appendix A: Config Schema

```yaml
# config.yaml additions
license:
  key: "POSTER-XXXXX-XXXXX-XXXXX-XXXXX"
  # Below are auto-populated after activation
  tier: "pro"
  max_accounts: 5
  expires_at: null
  cached_at: "2026-03-28T10:00:00Z"
  machine_id: "a1b2c3d4..."
```

---

## Appendix B: Database Schema (License Server)

```sql
-- Licenses table
CREATE TABLE licenses (
  id UUID PRIMARY KEY,
  key VARCHAR(30) UNIQUE NOT NULL,
  tier VARCHAR(20) NOT NULL,
  email VARCHAR(255),
  max_accounts INT DEFAULT 1,
  max_activations INT DEFAULT 1,
  expires_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT NOW(),
  revoked_at TIMESTAMP,
  notes TEXT
);

-- Activations table
CREATE TABLE activations (
  id UUID PRIMARY KEY,
  license_id UUID REFERENCES licenses(id),
  machine_id VARCHAR(64) NOT NULL,
  machine_name VARCHAR(255),
  app_version VARCHAR(20),
  activated_at TIMESTAMP DEFAULT NOW(),
  last_seen_at TIMESTAMP DEFAULT NOW(),
  deactivated_at TIMESTAMP,
  UNIQUE(license_id, machine_id)
);

-- Validation log (for analytics)
CREATE TABLE validation_log (
  id BIGSERIAL PRIMARY KEY,
  license_id UUID REFERENCES licenses(id),
  machine_id VARCHAR(64),
  result VARCHAR(20),
  ip_address INET,
  created_at TIMESTAMP DEFAULT NOW()
);
```
