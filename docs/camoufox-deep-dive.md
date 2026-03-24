# Camoufox Python Deep Dive (Installation + Practical Setup)

Source reviewed:
- https://camoufox.com/python/installation/
- https://camoufox.com/python/usage/
- https://camoufox.com/python/geoip/
- https://camoufox.com/python/config/

## 1. Installation Summary

1. Install package:
   - `pip install -U camoufox[geoip]`
2. Download browser binaries:
   - `camoufox fetch`
   - Alternative: `python -m camoufox fetch`
3. Verify / inspect:
   - `camoufox path`
   - `camoufox version`
4. Remove binaries:
   - `camoufox remove`

## 2. CLI Commands (from docs)

- `fetch`: Fetch latest Camoufox build
- `path`: Print Camoufox executable path
- `remove`: Remove downloaded files
- `server`: Launch Playwright server
- `test`: Open Playwright inspector
- `version`: Show version

## 3. Usage Integration Notes

- Camoufox is used as a Playwright-compatible browser/context wrapper.
- Keep defaults unless needed; docs caution against overriding low-level config manually.
- Important parameters for this PRD:
  - `headless`: keep `false` for realism in this workflow
  - `humanize`: available for cursor humanization
  - `geoip`: set `true` with proxy to align geolocation + locale + timezone
  - `proxy`: pass Playwright proxy dict when needed
  - `os`: can constrain generated fingerprint OS (`windows`, `macos`, `linux`)

## 4. Practical Recommendations for This Project

1. Start with no proxy for local validation.
2. Add `geoip=True` only when proxy is introduced.
3. Avoid passing raw `config` overrides unless strictly required.
4. Keep a canary-run strategy:
   - Small group set
   - Manual observation
   - Review logs/screenshots before scaling

## 5. Standardized Bootstrap for This Scaffold

```bash
pip install -U -r requirements.txt
camoufox fetch
python main.py --setup
python main.py --run-once
```
