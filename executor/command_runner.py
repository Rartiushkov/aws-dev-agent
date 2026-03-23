import os
import shlex
import subprocess


DANGEROUS_SHELL_TOKENS = ("&&", "||", ";", "|", ">", "<", "`", "\n", "\r")


def tokenize_command(cmd):
    if isinstance(cmd, (list, tuple)):
        tokens = [str(item) for item in cmd if str(item)]
    elif isinstance(cmd, str):
        if any(token in cmd for token in DANGEROUS_SHELL_TOKENS):
            raise ValueError(f"Unsafe shell token detected in command: {cmd}")
        tokens = shlex.split(cmd, posix=os.name != "nt")
    else:
        raise TypeError("cmd must be a string or sequence of args")

    if not tokens:
        raise ValueError("Command cannot be empty")
    return tokens


def run_command(cmd, **kwargs):
    return subprocess.run(tokenize_command(cmd), shell=False, **kwargs)
