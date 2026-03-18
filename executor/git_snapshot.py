import subprocess
import datetime


def save_snapshot(message="agent update"):

    timestamp = datetime.datetime.utcnow().isoformat()

    commit_message = f"{message} | {timestamp}"

    try:
        subprocess.run("git add .", shell=True)
        subprocess.run(f'git commit -m "{commit_message}"', shell=True)
        subprocess.run("git push", shell=True)

        print("📦 Git snapshot saved")

    except Exception as e:
        print("⚠️ Git snapshot failed:", str(e))