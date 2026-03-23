import subprocess
import json

from executor.command_runner import tokenize_command


class LogScanner:

    def run(self, cmd):

        result = subprocess.run(
            tokenize_command(cmd) if isinstance(cmd, str) else cmd,
            shell=False,
            capture_output=True,
            text=True
        )

        return result.stdout


    def scan_lambda_logs(self, lambda_name):
        cmd = ["aws", "logs", "tail", f"/aws/lambda/{lambda_name}", "--since", "5m"]

        result = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True
        )

        return result.stdout
