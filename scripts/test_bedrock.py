import os
from pathlib import Path

import boto3
from botocore.config import Config
from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[1]
load_dotenv(project_root / ".env")

required = [
    "AWS_REGION",
    "BEDROCK_MODEL_ID",
    "AWS_BEARER_TOKEN_BEDROCK",
]

missing = [name for name in required if not os.getenv(name)]
if missing:
    raise SystemExit(f"Missing configuration: {', '.join(missing)}")

client = boto3.client(
    "bedrock-runtime",
    region_name=os.environ["AWS_REGION"],
    config=Config(
        connect_timeout=10,
        read_timeout=60,
        retries={"mode": "standard", "total_max_attempts": 2},
    ),
)

response = client.converse(
    modelId=os.environ["BEDROCK_MODEL_ID"],
    messages=[
        {
            "role": "user",
            "content": [
                {"text": "Say hello in one short sentence."}
            ],
        }
    ],
    inferenceConfig={"maxTokens": 100},
)

for block in response["output"]["message"]["content"]:
    if "text" in block:
        print(block["text"])

print("Token usage:", response.get("usage", {}))