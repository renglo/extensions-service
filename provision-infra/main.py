#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from provision_infra import cmd_apply, cmd_destroy, cmd_export, cmd_teardown  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="extensions-service provision-infra",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
actions:
  apply     Create AWS infra and write provision_manifest.json.
            Without --launch-type: Lambda IAM only (minimal manifest; deploy build = Lambda zip only).
            With --launch-type fargate|ec2: also ECR, S3, ECS cluster (subnets/SG from VPC).
            Options: --profile NAME, --launch-type fargate|ec2, --vpc vpc-xxxx,
                     --region REGION, --with-capacity,
                     --github-repo ORG/REPO (handlers OIDC; writes state/<ext>/handlers_github_oidc.json),
                     --enable-handlers-staging-role (second OIDC role for GitHub environment staging)
  destroy   Tear down EC2 capacity only (ASG/launch template/capacity provider). Cluster kept.
            Options: --profile NAME
  export    Print manifest values as KEY=VALUE for launcher/vars.json and write lambda_env_export.json.
  teardown  DESTRUCTIVE: Delete ALL AWS resources (IAM, ECR, S3, ECS, roles, policy, CloudWatch logs).
            Requires --yes to confirm. Also removes local state/<ext>/ directory.
            Options: --profile NAME, --region REGION, --yes, --keep-logs (preserve CloudWatch log groups)
""",
    )
    parser.add_argument("extension")
    parser.add_argument("action", choices=["apply", "destroy", "export", "teardown"])
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "apply":
        return cmd_apply(args.extension, args.rest)
    if args.action == "export":
        return cmd_export(args.extension, args.rest)
    if args.action == "teardown":
        return cmd_teardown(args.extension, args.rest)
    return cmd_destroy(args.extension, args.rest)


if __name__ == "__main__":
    raise SystemExit(main())
