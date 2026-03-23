import datetime
import subprocess


def save_snapshot(message="agent update"):

    timestamp = datetime.datetime.utcnow().isoformat()
    commit_message = f"{message} | {timestamp}"

    try:
        add_result = subprocess.run(["git", "add", "."], capture_output=True, text=True)
        if add_result.returncode != 0:
            print("Git add failed:", (add_result.stderr or "").strip())
            return

        commit_result = subprocess.run(
            ["git", "commit", "-m", commit_message],
            capture_output=True,
            text=True
        )

        commit_output = (commit_result.stdout or "") + (commit_result.stderr or "")
        if commit_result.returncode != 0:
            if "nothing to commit" in commit_output.lower():
                print("No git changes to snapshot")
                return
            print("Git commit failed:", commit_output.strip())
            return

        push_result = subprocess.run(["git", "push"], capture_output=True, text=True)
        if push_result.returncode != 0:
            print("Git push failed:", (push_result.stderr or "").strip())
            return

        print("Git snapshot saved")

    except Exception as e:
        print("Git snapshot failed:", str(e))
