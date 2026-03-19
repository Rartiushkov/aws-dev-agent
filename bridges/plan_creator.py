from executor.safe_mode import safe_fallback
import json


def create_plan(goal):

    goal = goal.lower().strip()

    # ===== IAM ROLE =====

    if "create iam role" in goal and "lambda" in goal:

        role_name = "lambda-basic-role"

        return [
            {
                "type": "command",
                "cmd": "echo {\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"lambda.amazonaws.com\"},\"Action\":\"sts:AssumeRole\"}]} > trust-policy.json"
            },
            {
                "type": "command",
                "cmd": f"aws iam create-role --role-name {role_name} --assume-role-policy-document file://trust-policy.json"
            },
            {
                "type": "command",
                "cmd": f"aws iam attach-role-policy --role-name {role_name} --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
            },
            {
                "type": "command",
                "cmd": "del trust-policy.json 2>nul"
            }
        ]

    # ===== LAMBDA CREATE =====

    if "create lambda" in goal:

        function_name = "hello-world-fn"
        role_arn = "arn:aws:iam::027087672282:role/lambda-basic-role"

        return [
            {
                "type": "command",
                "cmd": "echo def handler(event, context):\\n    return {'statusCode': 200, 'body': 'Hello World'} > lambda_function.py"
            },
            {
                "type": "command",
                "cmd": "powershell Compress-Archive -Path lambda_function.py -DestinationPath function.zip -Force"
            },
            {
                "type": "command",
                "cmd": f"aws lambda create-function --function-name {function_name} --runtime python3.11 --role {role_arn} --handler lambda_function.handler --zip-file fileb://function.zip"
            }
        ]

    # ===== SIMPLE COMMANDS =====

    if "list roles" in goal:
        return [{"type": "command", "cmd": "aws iam list-roles"}]

    if "list lambda" in goal:
        return [{"type": "command", "cmd": "aws lambda list-functions"}]

    # ===== AI =====

    try:
        from bedrock.bedrock_client import BedrockClient

        print("🧠 TRYING AI...")
        client = BedrockClient()

        response = client.ask(goal)

        print("🧠 RAW AI RESPONSE:", response)

        plan = json.loads(response)

        print("✅ AI PLAN PARSED")

        return plan

    except Exception as e:
        print("⚠️ AI FAILED:", str(e))

    print("🛡️ USING SAFE MODE")

    return safe_fallback(goal)
