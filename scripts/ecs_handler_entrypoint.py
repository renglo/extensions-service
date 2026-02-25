#!/usr/bin/env python3
"""
ECS task entrypoint: read payload from S3, run lambda_handler, write result to S3.
Env: REQUEST_ID, PAYLOAD_S3_BUCKET, PAYLOAD_S3_KEY, RESULT_S3_BUCKET, RESULT_S3_KEY.
"""
import json
import os
import sys

sys.path.insert(0, "/build/output")

def main():
    bucket_in = os.environ.get("PAYLOAD_S3_BUCKET")
    key_in = os.environ.get("PAYLOAD_S3_KEY")
    bucket_out = os.environ.get("RESULT_S3_BUCKET")
    key_out = os.environ.get("RESULT_S3_KEY")
    if not all([bucket_in, key_in, bucket_out, key_out]):
        result = {"statusCode": 500, "success": False, "error": "Missing S3 env vars"}
    else:
        try:
            import boto3
            s3 = boto3.client("s3")
            obj = s3.get_object(Bucket=bucket_in, Key=key_in)
            event = json.loads(obj["Body"].read().decode())
            from lambda_router import lambda_handler
            result = lambda_handler(event, None)
            s3.put_object(
                Bucket=bucket_out,
                Key=key_out,
                Body=json.dumps(result),
                ContentType="application/json",
            )
        except Exception as e:
            result = {"statusCode": 500, "success": False, "error": str(e)}
    # Also print so CloudWatch gets it
    print(json.dumps(result))
    sys.exit(0 if result.get("statusCode") == 200 else 1)

if __name__ == "__main__":
    main()
