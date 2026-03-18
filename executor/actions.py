import subprocess


def delete_all_policies(role_name):
    print(f"🧹 Deleting inline policies for role: {role_name}")

    try:
        list_cmd = f"aws iam list-role-policies --role-name {role_name}"
        result = subprocess.run(list_cmd, shell=True, capture_output=True, text=True)

        if "NoSuchEntity" in result.stderr:
            print("ℹ️ Role does not exist — skipping")
            return

        import json
        policies = json.loads(result.stdout).get("PolicyNames", [])

        for policy in policies:
            delete_cmd = f"aws iam delete-role-policy --role-name {role_name} --policy-name {policy}"
            subprocess.run(delete_cmd, shell=True)

    except Exception as e:
        print("❌ Failed deleting policies:", str(e))