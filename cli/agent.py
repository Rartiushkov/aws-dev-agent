from bridges.plan_creator import create_plan
from executor.action_executor import execute_action
from agents.error_detector import detect_error
from executor.git_snapshot import save_snapshot

import os
import subprocess
import sys


DEFAULT_AWS_REGION = "us-east-1"


def build_command_env():
    env = os.environ.copy()
    env.setdefault("AWS_DEFAULT_REGION", DEFAULT_AWS_REGION)
    env.setdefault("AWS_REGION", DEFAULT_AWS_REGION)
    return env


def handle_aws_cli_error(result):

    stderr = (result.stderr or "").strip()

    if not stderr:
        return {"action": "stop", "handled": False, "message": "Unknown command failure", "status": "failure"}

    print("ERROR:", stderr)

    err = stderr.lower()

    if "nosuchentity" in err:
        print("Nothing to delete; resource is already absent")
        return {"action": "continue", "handled": True, "message": "Resource already absent", "status": "success"}

    if "entityalreadyexists" in err:
        print("Resource already exists; skipping duplicate create")
        return {"action": "continue", "handled": True, "message": "Resource already exists", "status": "success"}

    if "resourceconflictexception" in err and "function already exist" in err:
        print("Lambda function already exists; continuing")
        return {"action": "continue", "handled": True, "message": "Lambda function already exists", "status": "success"}

    if "noregion" in err:
        print("AWS region is not configured")
        return {"action": "stop", "handled": True, "message": "AWS region is not configured", "status": "success"}

    if "nocredentials" in err or "unable to locate credentials" in err:
        print("AWS credentials are not configured")
        return {"action": "stop", "handled": True, "message": "AWS credentials are not configured", "status": "success"}

    if "accessdenied" in err or "access denied" in err:
        print("AWS access was denied; trying auto-fix plan")
        error = detect_error(stderr)
        if error and error.get("type") == "fix":
            print("AUTO FIX TRIGGERED")
            execute_fix_plan(error.get("plan", []))
            return {"action": "continue", "handled": True, "message": "Auto-fix executed for access issue", "status": "success"}
        return {"action": "stop", "handled": True, "message": stderr, "status": "failure"}

    if "throttling" in err or "rate exceeded" in err:
        print("AWS API throttled the request; stopping current plan")
        return {"action": "stop", "handled": True, "message": stderr, "status": "failure"}

    if "validationexception" in err or "paramvalidation" in err or "error parsing parameter" in err:
        print("AWS CLI rejected the command arguments")
        return {"action": "stop", "handled": True, "message": stderr, "status": "failure"}

    error = detect_error(stderr)

    if error:
        if error.get("type") == "ignore":
            return {"action": "continue", "handled": True, "message": "Ignored recoverable AWS error", "status": "success"}

        if error.get("type") == "retry":
            print("Retry-worthy AWS error detected; stopping current plan")
            return {"action": "stop", "handled": True, "message": stderr, "status": "failure"}

        if error.get("type") == "fix":
            print("AUTO FIX TRIGGERED")
            execute_fix_plan(error.get("plan", []))
            return {"action": "continue", "handled": True, "message": "Auto-fix executed", "status": "success"}

    if "an error occurred" in err:
        print("Unhandled AWS CLI error; stopping plan execution")
        return {"action": "stop", "handled": True, "message": stderr, "status": "failure"}

    return {"action": "stop", "handled": False, "message": stderr, "status": "failure"}


def execute_fix_plan(plan):

    for step in plan:

        if not isinstance(step, dict):
            continue

        print("Fix step:", step)

        if step.get("type") == "command":
            subprocess.run(step.get("cmd"), shell=True, env=build_command_env())

        elif step.get("type") == "action":
            execute_action(step)


def emit_agent_result(status, goal, details):
    print("===AGENT_RESULT_START===")
    print(f"status={status}")
    print(f"goal={goal}")
    print(f"details={details}")
    print("===AGENT_RESULT_END===")


def main():
    goal = None
    status = "failure"
    details = "Agent did not complete"

    if len(sys.argv) < 2:
        print("Usage: python -m cli.agent \"goal\"")
        emit_agent_result("failure", "", "Missing goal")
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
        emit_agent_result("failure", goal, "Invalid plan")
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
                text=True,
                env=build_command_env()
            )

            if result.stdout:
                print(result.stdout)

            if result.stderr and result.returncode == 0:
                print(result.stderr)

            if result.returncode != 0:
                error_result = handle_aws_cli_error(result)

                if error_result["action"] == "continue":
                    details = error_result["message"]
                    continue

                status = error_result.get("status", "failure")
                details = error_result["message"]
                break
        else:
            status = "success"
            details = "Plan execution finished"

        print("\nPlan execution finished")
        emit_agent_result(status, goal, details)

    finally:
        if goal:
            save_snapshot(goal)


if __name__ == "__main__":
    main()
