import json

from executor.command_runner import run_command


class AWSScanner:

    def run(self, cmd):

        result = run_command(
            cmd,
            capture_output=True,
            text=True
        )

        return result.stdout


    def scan(self):

        state = {}

        # Lambda
        lambdas_raw = self.run("aws lambda list-functions")

        try:
            lambdas_json = json.loads(lambdas_raw)
            state["lambdas"] = [
                f["FunctionName"]
                for f in lambdas_json.get("Functions", [])
            ]
        except:
            state["lambdas"] = []

        # DynamoDB
        tables_raw = self.run("aws dynamodb list-tables")

        try:
            tables_json = json.loads(tables_raw)
            state["dynamodb_tables"] = tables_json.get("TableNames", [])
        except:
            state["dynamodb_tables"] = []

        # SQS
        queues_raw = self.run("aws sqs list-queues")

        try:
            queues_json = json.loads(queues_raw)
            state["queues"] = queues_json.get("QueueUrls", [])
        except:
            state["queues"] = []

        # CloudFormation
        stacks_raw = self.run("aws cloudformation list-stacks")

        try:  
           stacks_json = json.loads(stacks_raw)
           state["stacks"] = [
           s["StackName"]
        for s in stacks_json.get("StackSummaries", [])
    ]
        except:
          state["stacks"] = []    

        return state
