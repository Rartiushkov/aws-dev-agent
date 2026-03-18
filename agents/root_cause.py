import re


def extract_missing_permission(error_text):

    if not error_text:
        return None

    match = re.search(r"perform:\s([a-zA-Z0-9:]+)", error_text)

    if match:
        return match.group(1)

    return None