import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from executor.scripts.export_lambda_code import export_lambda_artifacts, redact_environment


class FakeLambdaClient:
    def get_function(self, FunctionName):
        return {
            "Configuration": {
                "FunctionName": FunctionName,
                "FunctionArn": f"arn:aws:lambda:us-east-1:123456789012:function:{FunctionName}",
                "PackageType": "Zip",
                "Runtime": "python3.12",
                "Handler": "lambda_function.handler",
                "Environment": {
                    "Variables": {
                        "PUBLIC_SETTING": "ok",
                        "API_TOKEN": "secret-value",
                    }
                },
            },
            "Code": {
                "Location": "https://example.invalid/lambda.zip",
                "CodeSha256": "abc",
            },
        }


class ExportLambdaCodeTests(unittest.TestCase):
    def test_redact_environment_hides_sensitive_variable_names(self):
        config = {
            "Environment": {
                "Variables": {
                    "PASSWORD": "p",
                    "normal": "value",
                    "private_key": "key",
                }
            }
        }

        redacted = redact_environment(config)

        self.assertEqual(redacted["Environment"]["Variables"]["PASSWORD"], "[REDACTED]")
        self.assertEqual(redacted["Environment"]["Variables"]["private_key"], "[REDACTED]")
        self.assertEqual(redacted["Environment"]["Variables"]["normal"], "value")

    def test_export_lambda_artifacts_writes_zip_and_manifest(self):
        temp_dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        source_zip = temp_dir / "source.zip"
        with zipfile.ZipFile(source_zip, "w") as archive:
            archive.writestr("lambda_function.py", "def handler(event, context): return 'ok'")

        snapshot = {
            "source_env": "full-account-scan",
            "account_id": "123456789012",
            "region": "us-east-1",
            "lambda_functions": [{"FunctionName": "demo-worker"}],
        }

        manifest = export_lambda_artifacts(
            snapshot,
            FakeLambdaClient(),
            temp_dir / "lambda_code",
            downloader=lambda _location: source_zip,
        )

        exported = manifest["exported"][0]
        self.assertEqual(manifest["exported_count"], 1)
        self.assertTrue(Path(exported["zip_path"]).exists())
        self.assertTrue((temp_dir / "lambda_code" / "lambda_code_manifest.json").exists())
        config = json.loads(Path(exported["configuration_path"]).read_text(encoding="utf-8"))
        self.assertEqual(config["Environment"]["Variables"]["API_TOKEN"], "[REDACTED]")
        self.assertEqual(config["Environment"]["Variables"]["PUBLIC_SETTING"], "ok")


if __name__ == "__main__":
    unittest.main()
