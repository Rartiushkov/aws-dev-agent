import subprocess
import json


def run_command(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout, result.stderr


def delete_all_policies(role_name):

    print(f"🧹 Deleting inline policies for role: {role_name}")

    cmd = f"aws iam list-role-policies --role-name {role_name} --query 'PolicyNames' --output json"
    stdout, stderr = run_command(cmd)

    if stderr:
        print("ERROR:", stderr)

        err = stderr.lower()

        if "nosuchentity" in err:
            print("ℹ️ Role does not exist — skipping")
            return

        return

    try:
        policies = json.loads(stdout)
    except:
        print("Failed to parse policies")
        return

    print("Found policies:", policies)

    for policy_name in policies:
        print(f"👉 Deleting policy: {policy_name}")

        del_cmd = f"aws iam delete-role-policy --role-name {role_name} --policy-name {policy_name}"
        out, err = run_command(del_cmd)

        if err:
            print("ERROR:", err)
        else:
            print("Deleted:", policy_name)


def create_role(role_name):

    print(f"🆕 Creating role: {role_name}")

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
    stdout, stderr = run_command(cmd)

    if stderr:
        print("ERROR:", stderr)
    else:
        print(stdout)


def execute_action(step):

    action = step.get("action")

    if action == "DELETE_ALL_POLICIES":
        delete_all_policies(step.get("role_name"))

    elif action == "CREATE_ROLE":
        create_role(step.get("role_name"))

    else:
        print("⚠️ Unknown action:", action)