def detect_error(stderr):

    err = stderr.lower()

    if "accessdenied" in err:
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

    if "throttling" in err:
        return {
            "type": "retry"
        }

    return None