# AWS Dev Agent

Interactive AWS engineering agent.

Goal:
Replace manual shell copy-paste workflow with an AI-driven DevOps operator.

Architecture:

User
↓
Runner Agent
↓
Bedrock (Claude)
↓
Shell Executor
↓
AWS CLI / Git / Tests
↓
AWS Infrastructure

Modules:

runner/          orchestration loop
scanner/         reads AWS state
executor/        runs shell commands
bedrock/         Claude interface
state/           session state
prompts/         system prompts
configs/         config files# aws-dev-agent