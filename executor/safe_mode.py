import json
import subprocess


PROTECTED_PREFIXES = [
    "AWSServiceRole",
    "AmazonBedrock",
    "aws-service-role",
    "agent-",
    "plan-",
    "remediator-",
    "task-",
    "dispatcher-"
]

SAFE_DELETE_PREFIXES = [
    "test-",
    "tmp-",
    "demo-",
    "old-",
    "sandbox-"
]


def is_protected(role_name: str) -> bool:
    return any(role_name.startswith(p) for p in PROTECTED_PREFIXES)


def is_safe_to_delete(role_name: str) -> bool:
    return any(role_name.startswith(p) for p in SAFE_DELETE_PREFIXES)


def run_cmd(cmd):
    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True
    )
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
    }


def cleanup_roles():
    print("CLEANUP STARTED")

    result = run_cmd('aws iam list-roles --query "Roles[].RoleName"')

    if result["exit_code"] != 0:
        print("Failed to list roles:", result["stderr"])
        return []

    try:
        roles = json.loads(result["stdout"])
    except Exception:
        print("Failed to parse roles")
        return []

    actions = []

    for role in roles:

        if is_protected(role):
            print(f"Protected (skip): {role}")
            continue

        if not is_safe_to_delete(role):
            print(f"Not safe (skip): {role}")
            continue

        print(f"Safe delete: {role}")

        actions.append({
            "type": "action",
            "action": "DELETE_ALL_POLICIES",
            "role_name": role
        })

        actions.append({
            "type": "command",
            "cmd": f"aws iam delete-role --role-name {role}"
        })

    return actions


def safe_fallback(goal):

    if "cleanup roles" in goal:
        return cleanup_roles()

    return [
        {
            "type": "command",
            "cmd": f"echo SAFE MODE: no rule for '{goal}'"
        }
    ]
