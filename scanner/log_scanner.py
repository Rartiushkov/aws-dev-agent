import subprocess
import json


class LogScanner:

    def run(self, cmd):

        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True
        )

        return result.stdout


    def scan_lambda_logs(self, lambda_name):

        cmd = f"aws logs tail /aws/lambda/{lambda_name} --since 5m"

        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True
        )

        return result.stdout