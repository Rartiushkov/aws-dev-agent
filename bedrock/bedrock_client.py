import boto3


class BedrockClient:

    def __init__(self):
        self.client = boto3.client(
            "bedrock-runtime",
            region_name="us-east-1"
        )

        # список моделей (fallback)
        self.models = [
            "nvidia.nemotron-nano-12b-v2",
            "anthropic.claude-3-haiku-20240307-v1:0"
        ]

    def ask(self, prompt):

        last_error = None

        for model in self.models:
            try:
                print(f"🧠 Trying model: {model}")

                response = self.client.converse(
                    modelId=model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"text": prompt}
                            ]
                        }
                    ],
                    inferenceConfig={
                        "maxTokens": 200,
                        "temperature": 0.2
                    }
                )

                text = response["output"]["message"]["content"][0]["text"]

                print(f"✅ Model success: {model}")
                return text

            except Exception as e:
                print(f"❌ Model failed: {model} -> {str(e)}")
                last_error = e

        raise Exception(f"All models failed: {str(last_error)}")