from executor.command_runner import run_command


def execute_command(cmd):
    print(f"Executing: {cmd}")

    try:
        result = run_command(
            cmd,
            capture_output=True,
            text=True
        )

        if result.stdout:
            print(result.stdout)

        if result.stderr:
            print("ERROR:")
            print(result.stderr)

        return result

    except Exception as e:
        print("Command failed:", str(e))
        return None


class ShellExecutor:

    def run(self, cmd):
        print(f"Executing: {cmd}")

        try:
            result = run_command(
                cmd,
                capture_output=True,
                text=True
            )

            if result.stdout:
                print(result.stdout)

            if result.stderr:
                print("ERROR:")
                print(result.stderr)

            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
            }

        except Exception as e:
            print("Command failed:", str(e))
            return {
                "stdout": "",
                "stderr": str(e),
                "exit_code": 1,
            }
