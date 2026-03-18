ALLOWED_PREFIX = [
"aws lambda",
"aws logs",
"aws dynamodb",
"aws sqs",
"aws cloudwatch",
"aws cloudformation",
"aws sts"
]

BLOCKED_WORDS = [
"delete",
"remove",
"rm ",
"terminate",
"destroy",
"shutdown",
"reboot"
]


def is_safe(command):

    command = command.lower().strip()

    # запрещённые слова
    for bad in BLOCKED_WORDS:
        if bad in command:
            return False

    # разрешённые команды
    for allowed in ALLOWED_PREFIX:
        if command.startswith(allowed):
            return True

    return False