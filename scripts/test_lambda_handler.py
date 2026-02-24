#!/usr/bin/env python3
"""
Invoke extension Lambda handler (shared script).
Requires: EXTENSION_NAME, WORKSPACE_ROOT; LAMBDA_FUNCTION_NAME set by run.py.
Usage: test_lambda_handler.py <handler_name> [--payload-file file.json] [--payload '{}'] [--function name] [--region r]
"""
import argparse
import json
import os
import sys
from typing import Any, Dict

import boto3


def invoke_handler(
    handler_name: str,
    payload: Dict[str, Any] = None,
    function_name: str = None,
    region: str = "us-east-1",
) -> Dict[str, Any]:
    if payload is None:
        payload = {}
    function_name = function_name or os.environ.get("LAMBDA_FUNCTION_NAME", "")
    if not function_name:
        print("ERROR: Lambda function name unknown (set LAMBDA_FUNCTION_NAME or use --function)", file=sys.stderr)
        return {"error": "LAMBDA_FUNCTION_NAME not set"}

    lambda_client = boto3.client("lambda", region_name=region)
    event = {"handler": handler_name, "payload": payload}

    print(f"Invoking handler: {handler_name}")
    print(f"Function: {function_name}")
    print(f"Region: {region}")
    print("-" * 60)

    try:
        response = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(event),
        )
        response_payload = json.loads(response["Payload"].read())
        if "FunctionError" in response:
            print(f"ERROR: {response['FunctionError']}", file=sys.stderr)
            print(response_payload, file=sys.stderr)
            return response_payload
        print(json.dumps(response_payload, indent=2))
        return response_payload
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return {"error": str(e)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Invoke extension Lambda handler")
    parser.add_argument("handler", help="Handler name")
    parser.add_argument("--payload-file", type=str, help="JSON payload file")
    parser.add_argument("--payload", type=str, help="JSON payload string")
    parser.add_argument(
        "--function",
        type=str,
        default=os.environ.get("LAMBDA_FUNCTION_NAME", ""),
        help="Lambda function name",
    )
    parser.add_argument("--region", type=str, default="us-east-1", help="AWS region")
    args = parser.parse_args()

    payload = {}
    if args.payload_file:
        with open(args.payload_file) as f:
            payload = json.load(f)
    elif args.payload:
        payload = json.loads(args.payload)

    result = invoke_handler(
        handler_name=args.handler,
        payload=payload,
        function_name=args.function or None,
        region=args.region,
    )
    if isinstance(result, dict) and (result.get("success") is False or "error" in result):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
