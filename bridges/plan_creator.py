from bridges.dynamo_writer import send_plan
from executor.safe_mode import safe_fallback
import json
import re
import time


FEATURE_TEST_PATTERNS = {
    "lambda": "test_plan_creator.py",
    "cloudwatch": "test_runner_cloudwatch.py",
    "runner": "test_runner_cloudwatch.py",
    "plan": "test_plan_creator.py",
}

CODEBUILD_PROJECT_NAME = "aws-dev-agent-tests"
DEFAULT_LAMBDA_ROLE_ARN = "arn:aws:iam::027087672282:role/lambda-basic-role"
LAMBDA_ROLE_SCOPES = {
    "basic": DEFAULT_LAMBDA_ROLE_ARN,
}


def extract_clone_request(raw_goal):
    source_cluster_match = re.search(r"cluster(?:\s+named)?\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
    source_service_match = re.search(r"service(?:\s+named)?\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
    target_env_match = re.search(r"to\s+(?:new\s+)?(?:environment|env)(?:\s+named)?\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
    team_match = re.search(r"team(?:\s+named)?\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)

    return {
        "source_cluster": source_cluster_match.group(1) if source_cluster_match else "",
        "source_service": source_service_match.group(1) if source_service_match else "",
        "target_env": target_env_match.group(1) if target_env_match else "",
        "team": team_match.group(1) if team_match else "",
    }


def autotest_step(goal_text=None):
    goal_text = (goal_text or "").lower()

    for feature, pattern in FEATURE_TEST_PATTERNS.items():
        if feature in goal_text:
            return {
                "type": "command",
                "cmd": f"python -m unittest discover -s . -p \"{pattern}\""
            }

    return {
        "type": "command",
        "cmd": "python -m unittest discover -s . -p \"test_*.py\""
    }


def append_autotest(plan, goal_text=None):
    extended_plan = list(plan)
    extended_plan.append(autotest_step(goal_text))
    return extended_plan


def extract_lambda_name(raw_goal):

    match = re.search(r"named\s+(.+?)(?:\s+test lambda|\s+for integration|\s+integration|$)", raw_goal, re.IGNORECASE)

    if not match:
        return "hello-world-fn"

    candidate = match.group(1).strip().lower()
    candidate = re.sub(r"[^a-z0-9-_]+", "-", candidate)
    candidate = re.sub(r"-{2,}", "-", candidate).strip("-_")

    if not candidate:
        return "hello-world-fn"

    return candidate[:64]


def lambda_template_source(template_id):
    template_id = (template_id or "hello-world").strip().lower()
    templates = {
        "hello-world": "\n".join([
            "def handler(event, context):",
            '    return {"statusCode": 200, "body": "Hello World"}',
        ]),
        "api-handler": "\n".join([
            "import json",
            "",
            "def handler(event, context):",
            '    payload = {"ok": True, "path": event.get("rawPath") or event.get("path", "")}',
            '    return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": json.dumps(payload)}',
        ]),
        "sqs-consumer": "\n".join([
            "def handler(event, context):",
            '    records = event.get("Records", [])',
            "    processed = []",
            "    for record in records:",
            '        processed.append(record.get("messageId", "unknown"))',
            '    return {"processed": processed, "count": len(processed)}',
        ]),
        "scheduled-task": "\n".join([
            "from datetime import datetime, timezone",
            "",
            "def handler(event, context):",
            '    return {"status": "scheduled", "ran_at": datetime.now(timezone.utc).isoformat()}',
        ]),
    }
    return templates.get(template_id, templates["hello-world"])


def powershell_set_content_command(path, content):
    lines = []
    for line in content.splitlines():
        lines.append("'" + line.replace("'", "''") + "'")
    return f'powershell -Command "Set-Content -Path {path} -Value @({",".join(lines)})"'


def resolve_lambda_role_arn(iam_scope="basic", role_arn=""):
    if role_arn:
        return role_arn
    return LAMBDA_ROLE_SCOPES.get((iam_scope or "basic").strip().lower(), DEFAULT_LAMBDA_ROLE_ARN)


def build_create_lambda_plan(
    function_name,
    runtime="python3.11",
    template_id="hello-world",
    iam_scope="basic",
    role_arn="",
    trigger_type="none",
    trigger_source="",
    include_test=False,
):
    function_name = extract_lambda_name(f"named {function_name}")
    role_arn = resolve_lambda_role_arn(iam_scope, role_arn)
    source = lambda_template_source(template_id)
    plan = [
        {
            "type": "command",
            "cmd": powershell_set_content_command("lambda_function.py", source),
        },
        {
            "type": "command",
            "cmd": "powershell Compress-Archive -Path lambda_function.py -DestinationPath function.zip -Force"
        },
        {
            "type": "command",
            "cmd": f"aws lambda create-function --function-name {function_name} --runtime {runtime} --role {role_arn} --handler lambda_function.handler --zip-file fileb://function.zip"
        },
        {
            "type": "command",
            "cmd": f"aws lambda wait function-active-v2 --function-name {function_name}"
        },
        {
            "type": "command",
            "cmd": f"aws lambda update-function-code --function-name {function_name} --zip-file fileb://function.zip"
        },
        {
            "type": "command",
            "cmd": f"aws lambda wait function-updated --function-name {function_name}"
        }
    ]

    trigger_type = (trigger_type or "none").strip().lower()
    if trigger_type == "sqs" and trigger_source:
        plan.append({
            "type": "command",
            "cmd": f"aws lambda create-event-source-mapping --function-name {function_name} --event-source-arn {trigger_source}"
        })
    elif trigger_type == "schedule" and trigger_source:
        schedule_rule = f"{function_name}-schedule"
        statement_id = f"{function_name}-schedule-invoke"
        plan.extend([
            {
                "type": "command",
                "cmd": f'aws events put-rule --name {schedule_rule} --schedule-expression "{trigger_source}"'
            },
            {
                "type": "command",
                "cmd": f"powershell -Command \"$account = aws sts get-caller-identity --query Account --output text; aws lambda add-permission --function-name {function_name} --statement-id {statement_id} --action lambda:InvokeFunction --principal events.amazonaws.com --source-arn arn:aws:events:$env:AWS_REGION:$account:rule/{schedule_rule}\""
            },
            {
                "type": "command",
                "cmd": f"powershell -Command \"$arn = aws lambda get-function --function-name {function_name} --query Configuration.FunctionArn --output text; aws events put-targets --rule {schedule_rule} --targets '[{{\\\"Id\\\":\\\"1\\\",\\\"Arn\\\":\\\"' + $arn + '\\\"}}]'\""
            },
        ])

    if include_test:
        plan.extend([
            {
                "type": "command",
                "cmd": f"aws lambda invoke --function-name {function_name} response.json"
            },
            {
                "type": "command",
                "cmd": "type response.json"
            }
        ])
    return plan


def create_plan(goal):

    raw_goal = goal.strip()
    goal = raw_goal.lower()
    function_name = extract_lambda_name(raw_goal)
    wants_lambda_test = "test lambda" in goal or "integration" in goal

    if "create iam role" in goal and "lambda" in goal:

        role_name = "lambda-basic-role"

        return append_autotest([
            {
                "type": "command",
                "cmd": "powershell -Command \"Set-Content -Path trust-policy.json -Value '{\\\"Version\\\":\\\"2012-10-17\\\",\\\"Statement\\\":[{\\\"Effect\\\":\\\"Allow\\\",\\\"Principal\\\":{\\\"Service\\\":\\\"lambda.amazonaws.com\\\"},\\\"Action\\\":\\\"sts:AssumeRole\\\"}]}'\""
            },
            {
                "type": "command",
                "cmd": f"aws iam create-role --role-name {role_name} --assume-role-policy-document file://trust-policy.json"
            },
            {
                "type": "command",
                "cmd": f"aws iam attach-role-policy --role-name {role_name} --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
            },
            {
                "type": "command",
                "cmd": "del trust-policy.json 2>nul"
            }
        ], goal)

    if "create lambda" in goal:
        plan = build_create_lambda_plan(function_name, include_test=wants_lambda_test)
        return append_autotest(plan, goal)

    if "test lambda" in goal or "integration" in goal:
        return [
            {
                "type": "command",
                "cmd": f"aws lambda invoke --function-name {function_name} response.json"
            },
            {
                "type": "command",
                "cmd": "type response.json"
            }
        ]

    if "list roles" in goal:
        return [{"type": "command", "cmd": "aws iam list-roles"}]

    if "list lambda" in goal:
        return [{"type": "command", "cmd": "aws lambda list-functions"}]

    if "run autotest in aws" in goal or "run tests in aws" in goal:
        return [
            {
                "type": "command",
                "cmd": f"aws codebuild start-build --project-name {CODEBUILD_PROJECT_NAME}"
            }
        ]

    if "discover environment" in goal or "scan environment" in goal or "scan aws environment" in goal:
        source_env_match = re.search(r"(?:named|for)\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
        source_env = source_env_match.group(1) if source_env_match else ""
        command = "python executor/scripts/discover_aws_environment.py"
        if source_env:
            command += f" --source-env {source_env}"
        team_match = re.search(r"team(?:\s+named)?\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
        if team_match:
            command += f" --team {team_match.group(1)}"
        return append_autotest([{"type": "command", "cmd": command}], goal)

    if "scan risks" in goal or "risk scan" in goal:
        source_env_match = re.search(r"(?:named|for)\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
        source_env = source_env_match.group(1) if source_env_match else ""
        command = "python executor/scripts/scan_environment_risks.py"
        if source_env:
            command += f" --source-env {source_env}"
        return append_autotest([{"type": "command", "cmd": command}], goal)

    if "migration strategy" in goal or "build strategy" in goal:
        source_env_match = re.search(r"(?:named|for)\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
        source_env = source_env_match.group(1) if source_env_match else ""
        command = "python executor/scripts/build_migration_strategy.py"
        if source_env:
            command += f" --source-env {source_env}"
        return append_autotest([{"type": "command", "cmd": command}], goal)

    if "transfer s3 objects" in goal or "s3 transfer" in goal:
        source_env_match = re.search(r"(?:named|for)\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
        source_env = source_env_match.group(1) if source_env_match else ""
        command = "python executor/scripts/transfer_s3_objects.py"
        if source_env:
            command += f" --source-env {source_env}"
        return append_autotest([{"type": "command", "cmd": command}], goal)

    if "network migration plan" in goal or "build network plan" in goal:
        source_env_match = re.search(r"(?:named|for)\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
        source_env = source_env_match.group(1) if source_env_match else ""
        command = "python executor/scripts/build_network_migration_plan.py"
        if source_env:
            command += f" --source-env {source_env}"
        return append_autotest([{"type": "command", "cmd": command}], goal)

    if "analyze kms" in goal or "kms usage" in goal:
        source_env_match = re.search(r"(?:named|for)\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
        source_env = source_env_match.group(1) if source_env_match else ""
        command = "python executor/scripts/analyze_kms_usage.py"
        if source_env:
            command += f" --source-env {source_env}"
        return append_autotest([{"type": "command", "cmd": command}], goal)

    if "iac blueprint" in goal or "export iac" in goal:
        source_env_match = re.search(r"(?:named|for)\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
        source_env = source_env_match.group(1) if source_env_match else ""
        command = "python executor/scripts/export_iac_blueprint.py"
        if source_env:
            command += f" --source-env {source_env}"
        return append_autotest([{"type": "command", "cmd": command}], goal)

    if "backup git" in goal or "git backup" in goal:
        source_env_match = re.search(r"(?:named|for)\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
        source_env = source_env_match.group(1) if source_env_match else ""
        command = "python executor/scripts/backup_git_repos.py"
        if source_env:
            command += f" --source-env {source_env}"
        return append_autotest([{"type": "command", "cmd": command}], goal)

    if "test git connection" in goal:
        config_match = re.search(r"(?:config|using)\s+([a-zA-Z0-9-_./\\:]+)", raw_goal, re.IGNORECASE)
        command = "python executor/scripts/test_git_connection.py --config configs/transfer.example.json"
        if config_match:
            command = f"python executor/scripts/test_git_connection.py --config {config_match.group(1)}"
        return append_autotest([{"type": "command", "cmd": command}], goal)

    if "export aws backup to git" in goal or "export backup to git" in goal:
        source_env_match = re.search(r"(?:named|for)\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
        config_match = re.search(r"(?:config|using)\s+([a-zA-Z0-9-_./\\:]+)", raw_goal, re.IGNORECASE)
        source_env = source_env_match.group(1) if source_env_match else ""
        command = "python executor/scripts/export_aws_backup_to_git.py"
        if source_env:
            command += f" --source-env {source_env}"
        if config_match:
            command += f" --config {config_match.group(1)}"
        command += " --init-git --commit"
        return append_autotest([{"type": "command", "cmd": command}], goal)

    if "destroy deployed env" in goal or "destroy target env" in goal:
        target_env_match = re.search(r"(?:env|environment)(?:\s+named)?\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
        if target_env_match:
            plan = [
                {
                    "type": "command",
                    "cmd": f"python executor/scripts/destroy_deployed_env.py --target-env {target_env_match.group(1)}"
                }
            ]
            return append_autotest(plan, goal)

    if "redeploy discovered env" in goal or "clean redeploy discovered env" in goal:
        source_env_match = re.search(r"(?:from|source)\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
        target_env_match = re.search(r"to\s+(?:new\s+)?(?:environment|env)(?:\s+named)?\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
        team_match = re.search(r"team(?:\s+named)?\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
        if source_env_match and target_env_match:
            plan = [
                {
                    "type": "command",
                    "cmd": f"python executor/scripts/destroy_deployed_env.py --target-env {target_env_match.group(1)}"
                },
                {
                    "type": "command",
                    "cmd": f"python executor/scripts/deploy_discovered_env.py --source-env {source_env_match.group(1)} --target-env {target_env_match.group(1)}" + (f" --team {team_match.group(1)}" if team_match else "")
                },
                {
                    "type": "command",
                    "cmd": f"python executor/scripts/validate_deployed_env.py --target-env {target_env_match.group(1)}"
                }
            ]
            return append_autotest(plan, goal)

    if "deploy discovered env" in goal or "deploy cloned env" in goal:
        source_env_match = re.search(r"(?:from|source)\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
        target_env_match = re.search(r"to\s+(?:new\s+)?(?:environment|env)(?:\s+named)?\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
        team_match = re.search(r"team(?:\s+named)?\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
        if source_env_match and target_env_match:
            plan = [
                {
                    "type": "command",
                    "cmd": f"python executor/scripts/deploy_discovered_env.py --source-env {source_env_match.group(1)} --target-env {target_env_match.group(1)}" + (f" --team {team_match.group(1)}" if team_match else "")
                },
                {
                    "type": "command",
                    "cmd": f"python executor/scripts/validate_deployed_env.py --target-env {target_env_match.group(1)}"
                }
            ]
            return append_autotest(plan, goal)

    if "clone env" in goal or "recreate env" in goal or "new env" in goal and "service" in goal and "cluster" in goal:
        clone_request = extract_clone_request(raw_goal)
        if clone_request["source_cluster"] and clone_request["source_service"] and clone_request["target_env"]:
            command = (
                f"python executor/scripts/clone_ecs_env.py "
                f"--source-cluster {clone_request['source_cluster']} "
                f"--source-service {clone_request['source_service']} "
                f"--target-env {clone_request['target_env']}"
            )
            if clone_request["team"]:
                command += f" --team {clone_request['team']}"
            return append_autotest([{"type": "command", "cmd": command}], goal)

    if "list codebuild projects" in goal or "list codebuild" in goal:
        return [
            {
                "type": "command",
                "cmd": "aws codebuild list-projects"
            }
        ]

    if "autotest" in goal or "run tests" in goal or "run autotest" in goal:
        return [
            autotest_step(goal)
        ]

    try:
        from bedrock.bedrock_client import BedrockClient

        print("TRYING AI...")
        client = BedrockClient()

        response = client.ask(goal)

        print("RAW AI RESPONSE:", response)

        plan = json.loads(response)

        print("AI PLAN PARSED")

        return plan

    except Exception as e:
        print("AI FAILED:", str(e))

    print("USING DEFAULT TEST PLAN")

    return [
        {
            "type": "command",
            "cmd": "echo running test"
        },
        {
            "type": "command",
            "cmd": "aws lambda list-functions"
        }
    ]


def create_fix_plan(description):

    timestamp = int(time.time())
    plan = {
        "pk": f"fix#{timestamp}",
        "sk": "latest",
        "status": "PENDING",
        "type": "AUTO_FIX",
        "createdAt": timestamp,
        "plan": {
            "target": {
                "role": "unknown"
            },
            "policy": {
                "Action": description[:200],
                "Resource": "*"
            }
        }
    }

    try:
        send_plan(plan)
    except Exception as e:
        print("Failed to send fix plan:", str(e))

    return plan
