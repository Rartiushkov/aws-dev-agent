from scanner.aws_scanner import AWSScanner
from executor.shell_executor import ShellExecutor
from executor.command_guard import is_safe
from state.state_manager import StateManager
from bedrock.bedrock_client import BedrockClient
from scanner.log_scanner import LogScanner

from agents.error_detector import detect_error
from agents.root_cause import extract_missing_permission
from bridges.plan_creator import create_fix_plan


class Runner:

    def __init__(self):

        self.scanner = AWSScanner()
        self.executor = ShellExecutor()
        self.state = StateManager()
        self.llm = BedrockClient()
        self.log_scanner = LogScanner()

    def inspect_cloudwatch_errors(self, aws_state):

        findings = []

        for fn in aws_state.get("lambdas", []):

            try:

                logs = self.log_scanner.scan_lambda_logs(fn)

                if not logs:
                    continue

                text = logs.lower()

                if "error" not in text and "exception" not in text and "accessdenied" not in text:
                    continue

                description = f"""
Lambda error detected from CloudWatch logs.

Lambda:
{fn}

Logs:
{logs[:1000]}
"""

                fix_plan = create_fix_plan(description)
                finding = {
                    "lambda": fn,
                    "source": "cloudwatch",
                    "message": "CloudWatch error detected; fix plan created and user should be notified",
                    "logs": logs[:1000],
                    "fix_plan": fix_plan,
                }
                findings.append(finding)

                print(f"CloudWatch error detected in {fn}")
                print("User notification: CloudWatch error found, AWS structure reviewed, fix plan created.")

            except Exception as e:
                print(f"Log scan failed for {fn}: {e}")

        return findings

    def run(self, goal: str):

        print("\n============================")
        print("AWS DEV AGENT")
        print("============================\n")

        print(f"Goal: {goal}\n")

        # ---------- SCAN AWS ----------
        print("Scanning AWS...\n")

        aws_state = self.scanner.scan()

        print("AWS Scan Result:")
        print(aws_state)

        # ---------- SCAN LAMBDA LOGS ----------
        print("\nScanning Lambda logs...\n")

        cloudwatch_findings = self.inspect_cloudwatch_errors(aws_state)

        if cloudwatch_findings:
            print("Fix plan created from CloudWatch logs.\n")

        # ---------- LOAD SYSTEM PROMPT ----------
        with open("prompts/system_prompt.txt") as f:
            system_prompt = f.read()

        prompt = f"""
{system_prompt}

Goal:
{goal}

AWS State:
{aws_state}
"""

        max_steps = 5

        for step in range(max_steps):

            print(f"\n------ STEP {step+1} ------\n")

            try:

                # ---------- ASK LLM ----------
                command = self.llm.ask(prompt)

                if not command:
                    print("LLM returned empty response")
                    break

                command = command.strip().split("\n")[0]

                print("LLM Suggested Command:")
                print(command)

                # ---------- FINISH ----------
                if command.lower() == "done":
                    print("\nAgent finished task.")
                    break

                # ---------- SECURITY ----------
                if not is_safe(command):
                    print("Blocked dangerous command")
                    break

                # ---------- PLACEHOLDER FILTER ----------
                if "<" in command or ">" in command:
                    print("⚠️ Skipping invalid LLM command (placeholder detected)")
                    continue

                # ---------- EXECUTE ----------
                print("\nExecuting command...\n")

                result = self.executor.run(command)

                print("Command Result:")
                print(result)

                stderr = (result.get("stderr") or "").lower()

                # ---------- CLI SYNTAX ERRORS ----------
                if "paramvalidation" in stderr or "error parsing parameter" in stderr:
                    print("⚠️ Skipping CLI syntax error (LLM issue)")
                    continue

                # ---------- WINDOWS / SHELL ERRORS ----------
                if "cannot find the file" in stderr:
                    print("⚠️ Skipping invalid command (Windows/syntax issue)")
                    continue

                # ==============================
                # 🔥 REAL ERROR HANDLER
                # ==============================
                if result.get("exit_code") != 0:

                    print("\n🔥 REAL ERROR → creating fix plan")

                    description = f"""
AWS CLI execution failed.

Goal:
{goal}

Command:
{command}

STDERR:
{result.get("stderr")}
"""

                    create_fix_plan(description)

                    print("✅ Fix plan created\n")

                    break

                # ---------- ERROR DETECTION ----------
                error = detect_error(result)

                if error:

                    print("\nDetected infrastructure problem:")
                    print(error)

                    permission = extract_missing_permission(str(result))

                    if permission:
                        print(f"Missing permission detected: {permission}")

                    description = f"""
AWS infrastructure error detected.

Goal:
{goal}

Command:
{command}

Detected Error:
{error}

Missing Permission:
{permission}

Raw Result:
{result}
"""

                    create_fix_plan(description)

                    print("\nFix plan created in DynamoDB.")

                    break

                # ---------- NEXT STEP ----------
                prompt = f"""
{system_prompt}

Goal:
{goal}

AWS State:
{aws_state}

Last command:
{command}

Result:
{result}

Suggest the next AWS CLI command or say DONE.
"""

            except Exception as e:

                print("\nAgent error occurred:")
                print(e)
                break

        # ---------- SAVE STATE ----------
        self.state.save(
            goal,
            aws_state,
            metadata={
                "cloudwatch_findings": cloudwatch_findings,
                "user_notification_required": bool(cloudwatch_findings),
            }
        )

        print("\nState saved.")
        print("\nAgent cycle finished.\n")
