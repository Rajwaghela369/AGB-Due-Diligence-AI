# Neon Managed Better Auth integration

Test configuration:

- `VITE_NEON_AUTH_URL=https://ep-morning-bread-b9txgrv6.neonauth.c-7.eu-central-1.aws.neon.tech/neondb/auth`

## Checklist

- [x] Add the Neon Auth client to the web application.
- [x] Add sign-in, sign-out, session state, and protected-route behavior.
- [x] Attach Neon access tokens to REST and SSE API requests.
- [x] Configure the FastAPI OIDC verifier for Neon values through environment overrides.
- [x] Keep `owner_id` mapped to the verified token subject (`sub`).
- [x] Confirm that Neon Auth's managed `neon_auth` schema needs no app migration.
- [x] Add focused frontend and backend-compatible auth tests.
- [ ] Run the live local auth flow against the test branch.
- [x] Document required Neon Console settings and local environment variables.

## Local activation

The integration is opt-in. Keep these values in the untracked `.env` while testing:

```env
VITE_NEON_AUTH_URL=https://<branch>.neonauth.<region>.aws.neon.tech/neondb/auth
VITE_NEON_AUTH_ENABLED=true
AGB_AUTH_ENABLED=true
NEON_AUTH_ISSUER=<issuer from Neon Console>
NEON_AUTH_AUDIENCE=<JWT audience from Neon Console>
NEON_AUTH_JWKS_URI=<JWKS URL from Neon Console>
```

Leave `VITE_NEON_AUTH_ENABLED` and `AGB_AUTH_ENABLED` set to `false` to preserve the
existing local auth-disabled behavior. Neon Console must allow the frontend origin
(`http://localhost:5173`) as a trusted origin, and email/password or an OAuth provider
must be enabled before sign-in can succeed. No `neon_auth` table is created or migrated
by this application.

## Database decision

Neon Managed Better Auth owns the `neon_auth` schema. Do not duplicate its user,
session, account, or verification tables in the application migrations. The existing
application `owner_id` columns remain the ownership boundary and store the verified
Neon user subject.

## Decision-router follow-up

- [x] Replace the AGB semantic topic router with a typed Gemini Decisions choice.
- [x] Charge routing through the shared request limiter.
- [x] Fall back to broad retrieval when routing fails.
- [x] Reuse the review-start topic inside the graph to avoid duplicate route calls.
- [x] Keep Jina for legal-query embeddings and AVV multi-label obligation routing.
- [ ] Benchmark routing quality and cost before enabling a live evaluation run.
