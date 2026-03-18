import subprocess


def execute_command(cmd):
    print(f"👉 Executing: {cmd}")

    try:
        result = subprocess.run(
            cmd,
            shell=True,
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
        print("❌ Command failed:", str(e))