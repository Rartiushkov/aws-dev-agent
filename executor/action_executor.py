import json
import subprocess


def run_command(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
    }


def delete_all_policies(role_name):

    print(f"Deleting inline policies for role: {role_name}")

    cmd = f"aws iam list-role-policies --role-name {role_name} --query 'PolicyNames' --output json"
    result = run_command(cmd)
    stdout = result["stdout"]
    stderr = result["stderr"]

    if result["exit_code"] != 0:
        print("ERROR:", stderr)

        err = stderr.lower()

        if "nosuchentity" in err:
            print("Role does not exist; skipping")
            return

        return

    try:
        policies = json.loads(stdout)
    except Exception:
        print("Failed to parse policies")
        return

    print("Found policies:", policies)

    for policy_name in policies:
        print(f"Deleting policy: {policy_name}")

        del_cmd = f"aws iam delete-role-policy --role-name {role_name} --policy-name {policy_name}"
        delete_result = run_command(del_cmd)

        if delete_result["exit_code"] != 0:
            print("ERROR:", delete_result["stderr"])
        else:
            print("Deleted:", policy_name)


def create_role(role_name):

    print(f"Creating role: {role_name}")

    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }
        ]
    }

    with open("trust-policy.json", "w") as f:
        json.dump(trust_policy, f)

    cmd = f"aws iam create-role --role-name {role_name} --assume-role-policy-document file://trust-policy.json"
    result = run_command(cmd)

    if result["exit_code"] != 0:
        print("ERROR:", result["stderr"])
    else:
        print(result["stdout"])


def execute_action(step):

    action = step.get("action")

    if action == "DELETE_ALL_POLICIES":
        delete_all_policies(step.get("role_name"))

    elif action == "CREATE_ROLE":
        create_role(step.get("role_name"))

    else:
        print("Unknown action:", action)
