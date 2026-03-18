from bridges.plan_creator import create_plan
from executor.action_executor import execute_action
from agents.error_detector import detect_error
from executor.git_snapshot import save_snapshot

import subprocess
import sys


def execute_fix_plan(plan):

    for step in plan:

        if not isinstance(step, dict):
            continue

        print("🔧 Fix step:", step)

        if step.get("type") == "command":
            subprocess.run(step.get("cmd"), shell=True)

        elif step.get("type") == "action":
            execute_action(step)


def main():

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
        print("⚠️ Invalid plan")
        return

    for step in plan:
        print(step)

    print("\n============================")
    print("EXECUTING PLAN")
    print("============================\n")

    try:

        for step in plan:

            if not isinstance(step, dict):
                print("⚠️ Skipping invalid step:", step)
                continue

            print("⚙️ Step:", step)

            if step.get("type") == "action":
                execute_action(step)

            elif step.get("type") == "command":

                cmd = step.get("cmd")

                print("👉 Executing:", cmd)

                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True
                )

                print(result.stdout)

                if result.stderr:
                    print("ERROR:", result.stderr)

                    err = result.stderr.lower()

                    if "nosuchentity" in err:
                        print("ℹ️ Nothing to delete — already removed")
                        continue

                    error = detect_error(result.stderr)

                    if error:

                        if error.get("type") == "ignore":
                            continue

                        if error.get("type") == "fix":
                            print("🔧 AUTO FIX TRIGGERED")
                            execute_fix_plan(error.get("plan", []))

        print("\n✅ Plan execution finished")

    finally:
        # 🔥 ВСЕГДА сохраняем состояние
        save_snapshot(goal)


if __name__ == "__main__":
    main()