import os
import sys


def main():
    prompt = " ".join(sys.argv[1:]).lower()
    if "username" in prompt:
        sys.stdout.write(os.environ.get("AWS_DEV_AGENT_GIT_USERNAME", "x-access-token"))
        return
    sys.stdout.write(os.environ.get("AWS_DEV_AGENT_GIT_TOKEN", ""))


if __name__ == "__main__":
    main()
