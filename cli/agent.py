from bridges.plan_creator import create_plan
from executor.action_executor import execute_action
from agents.error_detector import detect_error
from executor.git_snapshot import save_snapshot

import subprocess
import sys


def handle_aws_cli_error(result):

    stderr = (result.stderr or "").strip()

    if not stderr:
        return {"action": "stop", "handled": False}

    print("ERROR:", stderr)

    err = stderr.lower()

    if "nosuchentity" in err:
        print("Nothing to delete; resource is already absent")
        return {"action": "continue", "handled": True}

    if "entityalreadyexists" in err:
        print("Resource already exists; skipping duplicate create")
        return {"action": "continue", "handled": True}

    if "accessdenied" in err or "access denied" in err:
        print("AWS access was denied; trying auto-fix plan")
        error = detect_error(stderr)
        if error and error.get("type") == "fix":
            print("AUTO FIX TRIGGERED")
            execute_fix_plan(error.get("plan", []))
            return {"action": "continue", "handled": True}
        return {"action": "stop", "handled": True}

    if "throttling" in err or "rate exceeded" in err:
        print("AWS API throttled the request; stopping current plan")
        return {"action": "stop", "handled": True}

    if "validationexception" in err or "paramvalidation" in err or "error parsing parameter" in err:
        print("AWS CLI rejected the command arguments")
        return {"action": "stop", "handled": True}

    error = detect_error(stderr)

    if error:
        if error.get("type") == "ignore":
            return {"action": "continue", "handled": True}

        if error.get("type") == "retry":
            print("Retry-worthy AWS error detected; stopping current plan")
            return {"action": "stop", "handled": True}

        if error.get("type") == "fix":
            print("AUTO FIX TRIGGERED")
            execute_fix_plan(error.get("plan", []))
            return {"action": "continue", "handled": True}

    if "an error occurred" in err:
        print("Unhandled AWS CLI error; stopping plan execution")
        return {"action": "stop", "handled": True}

    return {"action": "stop", "handled": False}


def execute_fix_plan(plan):

    for step in plan:

        if not isinstance(step, dict):
            continue

        print("Fix step:", step)

        if step.get("type") == "command":
            subprocess.run(step.get("cmd"), shell=True)

        elif step.get("type") == "action":
            execute_action(step)


def main():
    goal = None

    if len(sys.argv) < 2:
        print("Usage: python -m cli.agent \"goal\"")
        return

    goal = sys.argv[1]

    print("\n============================")
    print("AWS DEV AGENT")
    print("============================\n")

    print("Goal:", goal)

    plan = create_plan(goal)

    print("\nGenerated Plan:")

    if not isinstance(plan, list):
        print("Invalid plan")
        return

    for step in plan:
        print(step)

    print("\n============================")
    print("EXECUTING PLAN")
    print("============================\n")

    try:

        for step in plan:

            if not isinstance(step, dict):
                print("Skipping invalid step:", step)
                continue

            print("Step:", step)

            if step.get("type") == "action":
                execute_action(step)
                continue

            if step.get("type") != "command":
                print("Skipping unknown step type:", step.get("type"))
                continue

            cmd = step.get("cmd")

            if not cmd:
                print("Skipping command step without cmd")
                continue

            print("Executing:", cmd)

            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True
            )

            if result.stdout:
                print(result.stdout)

            if result.returncode != 0 or result.stderr:
                error_result = handle_aws_cli_error(result)

                if error_result["action"] == "continue":
                    continue

                break

        print("\nPlan execution finished")

    finally:
        if goal:
            save_snapshot(goal)


if __name__ == "__main__":
    main()
