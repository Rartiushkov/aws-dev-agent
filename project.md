This is an autonomous AWS DevOps agent.

Goal:
- Convert user requests into AWS CLI commands
- Execute commands safely
- Detect errors and auto-fix
- Deploy infrastructure automatically
- Save git snapshots

Architecture:
- cli/agent.py -> entrypoint
- bridges/plan_creator.py -> creates plans
- executor/ -> executes commands
- agents/error_detector.py -> detects errors

Plans:
- list of steps
- types: command, action, file

System must be self-healing.

Execution Loop:
- Always modify code directly in the repository
- After any code change, run `python -m cli.agent "<goal>"`
- Capture terminal output
- Analyze output only between `===AGENT_RESULT_START===` and `===AGENT_RESULT_END===`
- If there is any error, detect root cause, fix it, and rerun
- Repeat until success
- After success, commit changes automatically

Start working immediately after receiving a task.
Do not explain - act.
