# Security & Trust

This solution is designed to support a trust-first migration workflow.

## Operating Modes

- Read-only assessment
  - discovery
  - dependency graph
  - risk scan
  - migration strategy report
- Controlled deployment
  - deploy resources into a target environment
  - validate the result
  - generate manifests and audit logs

## Current Security Controls

- sanitized discovery snapshots for sensitive fields
- sanitized Git export artifacts
- generated backup artifacts excluded from Git
- audit execution log written to `state/audit/execution_log.jsonl`
- same-account same-region safety guard by default

## Recommended Client Trust Model

- run self-hosted in the client AWS account or client-controlled environment
- begin with a read-only role
- grant deploy role only for approved migration runs
- keep snapshots, reports, and logs in client-controlled storage

## Remaining Hardening Roadmap

- least-privilege IAM policy generation
- customer-managed KMS mapping
- stronger per-service smoke validation
- optional private backend deployment
