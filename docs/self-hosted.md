# Self-Hosted Deployment

## Recommended Model

- frontend or operator access outside the client account
- backend worker runs inside a client-controlled AWS account or VM
- AWS roles are assumed locally by the worker
- artifacts are stored in client-controlled storage

## Minimum Runtime

- Python
- AWS CLI / boto3
- Git
- access to this repository

## Suggested Modes

- local operator mode for development
- EC2-based backend worker for production
- CodeBuild-based execution for controlled client-side runs

## Why Self-Hosted Matters

- customer credentials stay in the customer perimeter
- easier security review
- easier legal/compliance approval
- stronger trust for IAM / Secrets Manager access
