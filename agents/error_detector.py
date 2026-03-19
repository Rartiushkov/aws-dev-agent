def detect_error(stderr):

    if isinstance(stderr, dict):
        stderr = stderr.get("stderr") or ""

    err = str(stderr).lower()

    if "accessdenied" in err or "access denied" in err:
        return {
            "type": "fix",
            "plan": [
                {
                    "type": "command",
                    "cmd": "aws iam list-attached-user-policies --user-name YOUR_USER"
                }
            ]
        }

    if "nosuchentity" in err:
        return {
            "type": "ignore"
        }

    if "throttling" in err or "rate exceeded" in err:
        return {
            "type": "retry"
        }

    return None
