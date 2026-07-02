# Project Memory

Use this file as the persistent short-term memory for the repo.
Update it when we learn something important that should survive across sessions.

## Current Stage

- This is not yet a polished product.
- Current value is the working automation engine and orchestration flow.
- Strongest wedge so far: cross-account / cross-region AWS environment discovery, recreation, and validation.
- The user's current AWS account after migration is `978184426928`.
- Older account `027087672282` should be treated as legacy and removed from active config when found.

## Validated Product Story

- The system already has a working engine for discovery, deploy, validation, and export workflows.
- The user states the current flow can move an environment in about 5 minutes for supported cases.
- There are live tests in the repo; this is not only a concept or mock demo.

## What Is Already Strong

- Discovery and dependency-aware inventory
- Cross-account access model through AssumeRole
- Controlled deploy flow into another account or region
- Validation and smoke checks after deploy
- Export of sanitized artifacts to Git

## What Is Not Ready Yet

- Product packaging and polished UX
- Enterprise trust/governance layer
- Broad claims for every AWS workload type
- Fully finished SaaS control plane

## Positioning Notes

- Do not frame this as a finished platform yet.
- Better framing: working migration engine, early product layer, strong startup wedge.
- Best pitch direction: compress AWS environment discovery + cross-account / cross-region recreation from hours or days into minutes.
- Same-region sandbox cloning is supported only with an explicit override (`allow_same_scope=true`) and should be treated as a controlled sandbox-only mode.

## Demo Notes

- Lead with the real engine, not with UI polish.
- Show the workflow: discovery -> plan -> deploy -> validate -> artifacts.
- Be careful with universal claims like "works for everything" or "nobody can do this".

## Recent User Intent

- User wants a persistent local memory file so future work does not require re-reviewing the same product context.
- Keep this file updated with durable project facts, not throwaway task chatter.

## Migration Tracks

Add one short block per migration track and keep it compact.

Template:

### Track: <client-or-project-name>

- Date:
- Source:
- Target:
- Scope:
- Access:
- Current status:
- Risks / blockers:
- Notes:
