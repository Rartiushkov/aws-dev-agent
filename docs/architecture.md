# ShellShift Architecture

## Current shape

ShellShift is a split system with:

- static frontend on Cloudflare Pages
- lightweight Python backend on Render
- Firebase Auth and Firestore for user identity and app data
- AWS discovery and migration scripts executed from the backend runtime

## Request flow

1. User signs in through Firebase Auth on `availabl.pages.dev`.
2. Frontend requests a Firebase ID token.
3. Frontend calls Render backend endpoints with `Authorization: Bearer <token>`.
4. Backend verifies the token with Firebase Auth REST API.
5. Backend either:
   - serves demo and billing routes
   - verifies AWS role access
   - creates and runs background scan jobs

## AWS execution model

ShellShift does not require deploying customer code into the client account for the managed model.

Instead:

- ShellShift backend holds its own AWS identity
- customer provides a trusted IAM role ARN
- backend assumes the customer role with STS
- discovery and migration scripts run remotely against AWS APIs

This model is appropriate for:

- cross-account assessment
- supported serverless and managed-service migrations
- workflows where direct AWS API access is enough

This model is weaker for:

- private-network-only systems
- systems requiring in-account agents
- non-supported service edge cases

## Runtime pieces

### Frontend

- `frontend/index.html`: product landing and live demo surface
- `frontend/login.html`: Firebase sign-in entry
- `frontend/connect.html`: role verification UX
- `frontend/onboarding.html`: connection capture and scan start
- `frontend/dashboard.html`: recent scan and migration evidence
- `frontend/migrations.html`: scan history and timeline view
- `frontend/_worker.js`: Cloudflare Pages worker proxy for Firebase auth helper paths

### Backend

- `render_backend.py`: HTTP server for auth, scan jobs, billing, demo payloads, and Cloudflare bridge
- `lambda_function.py`: Cloudflare DNS integration helper

### AWS engine

- `executor/scripts/discover_aws_environment.py`
- `executor/scripts/deploy_discovered_env.py`
- `executor/scripts/validate_deployed_env.py`
- `executor/scripts/migrate_account.py`
- `executor/scripts/transfer_s3_objects.py`

## Data model

### Firestore

- user profiles
- saved AWS connections
- migration metadata
- billing state

### Local runtime state

- scan job status is persisted under `state/runtime/scan_jobs.json`
- migration artifacts and inventory exports are persisted under `state/clients/...`

## Security model

Current strengths:

- Firebase-authenticated backend routes
- source access can remain read-only
- target access can be separated into a different role
- CORS narrowed to known frontend origins

Current weaknesses:

- backend process still mixes billing, demo, and scan orchestration concerns
- scan job persistence is file-based, not queue-backed
- Firebase and Google console settings remain external dependencies
- least-privilege IAM policy generation is still a roadmap item

## Near-term upgrade path

To move closer to production-grade architecture:

1. Split backend into auth, billing, and scan services.
2. Replace file-backed scan jobs with queue plus worker execution.
3. Add explicit job status polling and result documents in Firestore.
4. Move role verification and scan execution logs into structured audit records.
5. Publish a strict supported-services matrix and trust model.
