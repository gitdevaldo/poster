# Project Guidelines (IDE Environment)

> **⚠️ This file is dedicated to the IDE environment (VS Code + GitHub Copilot extension).**
> It applies when developing remotely from a local machine via SSH/IDE, where the VPS is a separate deployment target.
> For the Copilot CLI environment (developing directly on the server), see `copilot-instructions.md`.

The main project is in `indexnow-dev/`. Everything else (`main-old/`, `frontend-new/`, `ui-reference/`, `database-schema/`) is read-only reference material.

---

## Architecture

pnpm workspace monorepo (pnpm 10, Turborepo): three Next.js 16 apps + 10 shared packages.

```
indexnow-dev/
├── apps/
│   ├── api/              → Backend API (port 3001), all business logic as Route Handlers
│   ├── admin/            → Admin dashboard (port 3002), role-gated (admin/super_admin)
│   └── user-dashboard/   → User-facing dashboard (port 3000), auth-gated
├── packages/
│   ├── shared/           → @indexnow/shared — Zod schemas, types (DB source of truth), constants, utils
│   ├── database/         → @indexnow/database — Supabase client factories, security wrappers
│   ├── ui/               → @indexnow/ui — shadcn components, shared Tailwind config
│   ├── auth/             → @indexnow/auth — server auth, admin auth
│   ├── analytics/        → @indexnow/analytics — Sentry, PostHog
│   ├── mail/             → @indexnow/mail — SendGrid/SMTP
│   ├── services/         → @indexnow/services
│   ├── supabase-client/  → @indexnow/supabase-client — browser/server Supabase factories
│   ├── api-middleware/    → @indexnow/api-middleware
│   ├── eslint-config/    → @indexnow/eslint-config
│   └── tsconfig/         → Shared tsconfig presets
```

Frontends call `apps/api` via REST using `fetch()` with Supabase JWT Bearer tokens. No tRPC/GraphQL. All fetch calls include `credentials: 'include'` for cross-subdomain auth.

---

## Build and Test

```bash
pnpm install                                       # Install all workspace deps
pnpm build                                         # Turborepo: packages first (^build), then apps
pnpm dev                                           # All apps in parallel via Turborepo
pnpm --filter @indexnow/api dev                    # Single app (api on port 3001)
pnpm --filter @indexnow/shared build               # Single package (tsup → CJS + ESM + .d.ts)
pnpm type-check                                    # TypeScript noEmit check across all apps
pnpm test                                          # Vitest run
pnpm format                                        # Prettier format all files
```

**Build order is enforced by Turborepo** (`^build` dependency): packages build via tsup first, then apps via `next build --webpack`. Always rebuild packages before apps when changing shared code.

**Tests**: Vitest. Test files in `apps/api/src/__tests__/` and `packages/shared/src/__tests__/`.

---

## Code Style

- TypeScript strict mode, `"moduleResolution": "bundler"`, path alias `@/*` → `./src/*`
- All DB tables prefixed `indb_` (e.g., `indb_auth_user_profiles`)
- shadcn/ui "new-york" style with CSS variables; shared Tailwind theme in `@indexnow/ui/styles.css`
- Tailwind CSS 4, `tailwindcss-animate`, `@tailwindcss/typography`
- ESLint 9 with shared config from `@indexnow/eslint-config`
- `'use client'` directive on all interactive frontend components
- Prettier for formatting
- Husky for git hooks

---

## Project Conventions

### API Routes (`apps/api/src/app/api/v1/`)

Every route uses one of three wrappers from `@/lib/core/api-response-middleware.ts`:

```typescript
// Public — no auth (login, register, health, webhooks)
export const POST = publicApiWrapper(async (request: NextRequest, _context: RouteContext) => {
  const body = await request.json();
  const validation = loginSchema.safeParse(body);
  if (!validation.success) {
    const error = await ErrorHandlingService.createError(ErrorType.VALIDATION, '...', { ... });
    return formatError(error);
  }
  return formatSuccess(data);
});

// Authenticated — requires Bearer token, injects auth with userId + RLS-scoped Supabase client
export const GET = authenticatedApiWrapper(async (request: NextRequest, auth: AuthenticatedRequest, context: RouteContext) => {
  const result = await SecureServiceRoleWrapper.executeWithUserSession(
    asTypedClient(auth.supabase),
    { userId: auth.userId, operation: 'name', source: 'route/path', reason: 'why' },
    { table: 'indb_table_name', operationType: 'select' },
    async (db) => { /* queries using db client */ }
  );
  return formatSuccess(result);
});

// Admin — requires super_admin role, auto-logs security events
export const GET = adminApiWrapper(async (request: NextRequest, adminUser: AdminUser, context: RouteContext) => {
  const result = await SecureServiceRoleWrapper.executeSecureOperation(
    context, { table: 'indb_table', operationType: 'select' },
    async () => { /* queries using supabaseAdmin */ }
  );
  return formatSuccess(result);
});
```

**Note**: `RouteContext = { params: Promise<Record<string, string | string[]>> }` — Next.js 16 uses async params.

**Response shape** — always `formatSuccess(data)` / `formatError(error)`:
```json
{ "success": true, "data": {...}, "timestamp": "...", "requestId": "..." }
{ "success": false, "error": { "id": "uuid", "type": "VALIDATION_ERROR", "message": "...", "severity": "LOW", "statusCode": 400 } }
```

**Validation**: Zod schemas from `@indexnow/shared` — import via `import { loginSchema } from '@indexnow/shared'`.

**Error creation**: `ErrorHandlingService.createError(ErrorType.X, ...)` with `ErrorType` enum (VALIDATION, AUTHENTICATION, AUTHORIZATION, NOT_FOUND, DATABASE, EXTERNAL_API, BUSINESS_LOGIC, INTERNAL, RATE_LIMIT, SYSTEM, NETWORK, PAYMENT, ENCRYPTION). Common shortcuts via `CommonErrors.UNAUTHORIZED()`, `CommonErrors.NOT_FOUND()`, etc.

**DB writes**: Always through `SecureServiceRoleWrapper` which enforces user validation, input sanitization, and audit logging to `indb_security_audit_logs`.

### Frontend Apps

**user-dashboard** (port 3000):
- Uses `@tanstack/react-query` for data fetching (same as admin)
- Hooks in `src/lib/hooks.ts` use `useQuery`/`useMutation` with `fetch()` internally
- All hooks call `supabaseBrowser.auth.getSession()` then `fetch(endpoint, { headers: { 'Authorization': \`Bearer ${session.access_token}\` }, credentials: 'include' })`
- API endpoints centralized in `@/lib/core/constants/ApiEndpoints.ts`
- Settings pages use file-based routing: `settings/profile/`, `settings/billing/`, `settings/security/`, `settings/notifications/`

**admin** (port 3002):
- Uses `@tanstack/react-query` for data fetching
- Admin middleware verifies `admin`/`super_admin` role from `indb_auth_user_profiles.role` with IP-based rate limiting
- Settings pages: `settings/site/`, `settings/packages/`, `settings/payments/`

**Both apps**:
- Database client always from `@/lib/database` → re-exports `supabaseBrowser` from `@indexnow/database/client`. **Never import `@indexnow/database` directly in components.**
- `'use client'` directive on all interactive components
- Props defined as TypeScript `interface`

### Shared Packages

- **`@indexnow/shared`** — master barrel export. Sub-paths: `/schema`, `/types`, `/constants`, `/utils`
  - `schema.ts` (374 lines): 30+ Zod schemas including `loginSchema`, `registerSchema`, `createJobSchema`, `apiRequestSchemas.*`
  - `types/database.ts` (2136 lines): **THE source of truth** for all DB types. 30 `indb_*` tables with Row/Insert/Update types + convenience aliases (`DbUserProfile`, `InsertTransaction`, `UpdateUserProfile`, etc.)
  - `constants/`: `ApiEndpoints`, `AppConstants`, `ErrorMessages`, `ValidationRules`
  - `utils/`: async-utils, countries, currency, formatters, logger, rate-limiter, resilience, url-utils
- **`@indexnow/database`** — Supabase client factories + security wrappers
  - `/client`: `getBrowserClient()` (singleton browser client)
  - `/server`: `createAdminClient()` (service-role, bypasses RLS — **API only, inside SecureServiceRoleWrapper**)
  - `security/`: `SecureServiceRoleWrapper`, `SecureDatabaseHelpers`, `SecurityService`
- **`@indexnow/ui`** — shared shadcn/ui components + Tailwind config preset

### TypeScript Path Aliases

All apps extend `packages/tsconfig/nextjs.json` and map workspace packages:
```json
"@/*": ["./src/*"],
"@indexnow/shared": ["../../packages/shared/src"],
"@indexnow/shared/*": ["../../packages/shared/src/*"],
"@indexnow/database": ["../../packages/database/src"],
"@indexnow/database/client": ["../../packages/database/src/client"],
"@indexnow/ui": ["../../packages/ui/src"],
"@indexnow/auth": ["../../packages/auth/src"],
"@indexnow/analytics": ["../../packages/analytics/src"]
```

---

## Integration Points

| Service | Purpose | Key Env Vars |
|---------|---------|-------------|
| Supabase | Auth + PostgreSQL + Storage | `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY` |
| BullMQ/Redis | Job queue (lazy, `ENABLE_BULLMQ=true`) | `REDIS_URL` |
| Paddle | Payments | `PADDLE_API_KEY`, `PADDLE_WEBHOOK_SECRET` |
| SendGrid/SMTP | Email | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` |
| SeRanking | Rank tracking (`apps/api/src/lib/rank-tracking/`) | SE Ranking API keys |
| Sentry | Error monitoring (all apps) | `NEXT_PUBLIC_SENTRY_DSN` |
| PostHog | Product analytics | `NEXT_PUBLIC_POSTHOG_KEY` |
| Google API | Indexing (service accounts in DB) | `google-auth-library` |

All env vars listed in `.env.example` per app. **24 global env vars** configured in `turbo.json`.

---

## Security

- Auth: Supabase JWT in `Authorization: Bearer <token>` header
- Admin: role check from `indb_auth_user_profiles.role` (`admin`/`super_admin`)
- `SecureServiceRoleWrapper` enforces user validation + audit logging before any service-role DB operation. Two modes:
  - `executeWithUserSession()` — validates user session matches context, RLS-scoped
  - `executeSecureOperation()` — admin/system operations, sanitizes table/column names against SQL injection
- Full audit trail in `indb_security_audit_logs`
- Encryption via `ENCRYPTION_KEY` env var
- Rate limiting on auth endpoints (IP-based, Redis-backed)
- Security headers (CSP, HSTS, X-Frame-Options) configured in `next.config.ts`
- Never commit `.env` files

---

## Database

PostgreSQL via Supabase. **29 tables**, all prefixed `indb_`:

| Domain | Tables |
|--------|--------|
| Auth | `indb_auth_user_profiles`, `indb_auth_user_settings` |
| Payments | `indb_payment_gateways`, `indb_payment_packages`, `indb_payment_transactions`, `indb_payment_subscriptions` |
| Paddle | `indb_paddle_transactions`, `indb_paddle_webhook_events` |
| Keywords | `indb_keyword_bank`, `indb_keyword_countries`, `indb_keyword_domains`, `indb_keyword_rankings`, `indb_rank_keywords` |
| SeRanking | `indb_seranking_usage_logs`, `indb_seranking_metrics_raw`, `indb_seranking_metrics_aggregated`, `indb_seranking_quota_usage`, `indb_seranking_health_checks` |
| Admin | `indb_admin_activity_logs`, `indb_admin_user_summary` |
| Security | `indb_security_activity_logs`, `indb_security_audit_logs` |
| System | `indb_system_error_logs`, `indb_system_activity_logs`, `indb_site_settings`, `indb_site_integration` |
| Notifications | `indb_notifications_dashboard` |
| Jobs | `indb_enrichment_jobs` |

**Type source of truth**: `packages/shared/src/types/database.ts` (2136 lines) — NOT `packages/database/src/types.ts` (legacy re-export). All user-owned tables cascade-delete from `auth.users(id)`.

---

## Agent Behaviour Rules

### 1. No Lazy Fixes
- Always find and fix root causes. Never apply temporary workarounds or band-aids.
- When fixing a file, check all other files that import from or depend on the changed code. Trace the full impact.
- Senior developer standards: would a staff engineer approve this change?

### 2. Strict Type Safety — No `as` Casting
- **NEVER** use `as any`, `as unknown`, or any `as` type assertion. All values must use their real types.
- Database values must match types from `packages/shared/src/types/database.ts` (Row/Insert/Update types and convenience aliases like `DbUserProfile`, `InsertTransaction`, etc.).
- If a type mismatch exists, fix the type definition or the data flow — never cast around it.
- Zod schemas define input shapes; inferred types (`z.infer<typeof schema>`) are the source of truth for request/response bodies.

### 3. Skills and Lessons — Mandatory Pre-Task Reads
- **Before every task**, read `.claude/tasks/lessons.md` first. These are hard-won rules from past mistakes. Violating any lesson is a blocking issue.
- **Before every message and every to-do item**, scan `.claude/skills/` and load the skill files that match the current task. This is NOT optional — skills contain domain-specific procedures (adding routes, creating hooks, modifying schemas, writing PRDs, refactoring, git commits, etc.). Skipping skills leads to incorrect approaches.
  - At the start of each message: identify which skills apply → read their SKILL.md files → follow their procedures.
  - At the start of each to-do item: re-evaluate if additional skills are needed for the specific step.
  - Example: if committing code, load `git-commit` + `conventional-commit` skills. If writing a PRD, load the `prd` skill. If refactoring, load the `refactor` skill.
- Lessons define principles and verification steps that override default assumptions.

### 3b. Generated Documents — Always in `/docs` at Workspace Root
- **ALL generated markdown documents** (reports, deep dives, PRDs, implementation plans, audits, architecture docs) MUST be saved to the `/docs` folder at the workspace root (`c:\Users\Administrator\Desktop\indexnow-main\docs\`), **NOT** inside `indexnow-dev/`.
- `indexnow-dev/` is the project codebase — only code and code-adjacent files (config, tests) go there.
- Use descriptive filenames with date prefix when relevant: e.g., `docs/2026-03-06-api-structure-audit.md`.
- Create the `/docs` folder if it doesn't exist.

### 4. Workflow Orchestration

**Plan Mode**: Enter plan mode for any non-trivial task (3+ steps or architectural decisions). Write plan to `.claude/tasks/todo.md` with checkable items. If something goes sideways, STOP and re-plan immediately.

**Subagent Strategy**: Use subagents liberally for research, exploration, and parallel analysis. One task per subagent. Keep main context window clean.

**Self-Improvement Loop**: After ANY correction from the user, update `.claude/tasks/lessons.md` with the pattern and a rule to prevent recurrence. Review lessons at session start.

**Verification Before Done**: Never mark a task complete without proving it works. Run `pnpm type-check`, check for errors, demonstrate correctness. Diff behavior when relevant.

**Demand Elegance (Balanced)**: For non-trivial changes, pause and consider if there's a more elegant approach. Skip for simple, obvious fixes — don't over-engineer.

**Autonomous Bug Fixing**: When given a bug report, just fix it. Point at logs/errors/failing tests, then resolve. Zero hand-holding required from the user.

### 5. Task Management

1. Write plan to `.claude/tasks/todo.md` with checkable items
2. Mark items complete as you go
3. High-level summary at each step
4. Add review section to `.claude/tasks/todo.md` when done
5. Update `.claude/tasks/lessons.md` after corrections

### 6. Git Discipline

- **Scope**: Only commit and push inside `indexnow-dev/`. The root workspace and other folders (`main-old/`, `frontend-new/`, `ui-reference/`, `database-schema/`) are **never** committed.
- **Commit After Every Change**: After every change — even a single-line fix — immediately stage, commit, and push. No batching multiple unrelated changes. Keep the remote always up to date.
- **Use Git Skills**: Before committing, read and follow the relevant Git skills in `.claude/skills/` (e.g., `git-commit`, `conventional-commit`, `make-repo-contribution`). Generate conventional commit messages with proper type, scope, and description.
- **Workflow**:
  1. `cd indexnow-dev`
  2. Stage changed files (`git add`)
  3. Generate a conventional commit message using skill guidance
  4. `git commit` then `git push`
- **Remote**: `origin` → `https://github.com/ourcaldo/indexnow-mono.git`, branch `main`.

### 7. External Service Proxy Rule — Zero Direct Calls
- **NEVER** let browser/client code call external services directly (Supabase SDK, Paddle SDK, SendGrid, Google API, SE Ranking, etc.).
- ALL external service interactions MUST go through `apps/api` route handlers. The flow is always: `Frontend → apps/api → External Service`.
- **Allowed client-side exceptions** (Supabase only): `onAuthStateChange()`, `getSession()` (local read), `setSession()` (syncing tokens from API), `signOut({ scope: 'local' })` (clearing local state). Edge middleware is also exempt (server-side, can't proxy to API).
- When writing or reviewing any code that touches an external service, **verify the call goes through an API route**. If it doesn't, fix it.
- This applies to all packages consumed by frontend apps (`@indexnow/supabase-client`, `@indexnow/auth` client-side, `@indexnow/database/client`, etc.).

### 8. Core Principles

- **Simplicity First**: Make every change as simple as possible. Minimal code impact.
- **No Laziness**: Find root causes. No temporary fixes.
- **Minimal Impact**: Changes touch only what's necessary. Avoid introducing bugs.
- **Full Traceability**: When changing shared code, verify all consumers still work.
