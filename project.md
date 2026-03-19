This is an autonomous AWS DevOps agent.

Goal:
- Convert user requests into AWS CLI commands
- Execute commands safely
- Detect errors and auto-fix
- Deploy infrastructure automatically
- Save git snapshots

Architecture:
- cli/agent.py → entrypoint
- bridges/plan_creator.py → creates plans
- executor/ → executes commands
- agents/error_detector.py → detects errors

Plans:
- list of steps
- types: command, action, file

System must be self-healing.