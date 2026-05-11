#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from provision_infra import cmd_apply, cmd_destroy, cmd_export  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="extensions-service provision-infra",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
actions:
  apply    Create all AWS infra (IAM roles, ECR, S3, ECS cluster) and write provision_manifest.json.
           Subnets and security groups are auto-discovered from the VPC (default VPC unless --vpc is set).
           Options: --profile NAME, --launch-type fargate|ec2, --vpc vpc-xxxx,
                    --region REGION, --with-capacity
  destroy  Tear down EC2 capacity (ASG/launch template/capacity provider) and refresh manifest.
           Options: --profile NAME
  export   Print manifest values as KEY=VALUE for launcher/vars.json and write lambda_env_export.json.
""",
    )
    parser.add_argument("extension")
    parser.add_argument("action", choices=["apply", "destroy", "export"])
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "apply":
        return cmd_apply(args.extension, args.rest)
    if args.action == "export":
        return cmd_export(args.extension, args.rest)
    return cmd_destroy(args.extension, args.rest)


if __name__ == "__main__":
    raise SystemExit(main())
