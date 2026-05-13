"""Create or update GitHub OIDC IAM roles for the handlers (extensions-service) repository."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

from state_store import STATE_VERSION, utc_now_iso, write_json

OIDC_URL = "https://token.actions.githubusercontent.com"
OIDC_THUMBPRINT = "6938fd4d98bab03faadb97b34396831e3780aea1"
HANDLERS_OIDC_DESCRIPTION = "GitHub Actions handlers ECS deploy"

_UTILS_DIR = Path(__file__).resolve().parent / "utils"
_TRUST_TEMPLATE = _UTILS_DIR / "github-handlers-oidc-trust.template.json"
_POLICY_TEMPLATE = _UTILS_DIR / "github-handlers-actions-policy.template.json"


@dataclass
class HandlersBootstrapConfig:
    extension: str
    aws_profile: str | None
    aws_region: str
    github_repo: str
    enable_staging_role: bool = False
    ecs_results_bucket: str | None = None
    apply_changes: bool = True
    state_out_path: Path | None = None


def _session(profile: str | None, region: str) -> boto3.Session:
    kwargs: dict[str, Any] = {"region_name": region}
    if profile:
        kwargs["profile_name"] = profile
    return boto3.Session(**kwargs)


def _oidc_provider_arn(account_id: str) -> str:
    return f"arn:aws:iam::{account_id}:oidc-provider/token.actions.githubusercontent.com"


def _render_template(template_path: Path, replacements: dict[str, str]) -> dict[str, Any]:
    text = template_path.read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace("{{" + key + "}}", value)
    return json.loads(text)


def _ensure_oidc_provider(iam, oidc_provider_arn: str, apply_changes: bool) -> None:
    try:
        iam.get_open_id_connect_provider(OpenIDConnectProviderArn=oidc_provider_arn)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
            raise
        if apply_changes:
            iam.create_open_id_connect_provider(
                Url=OIDC_URL,
                ClientIDList=["sts.amazonaws.com"],
                ThumbprintList=[OIDC_THUMBPRINT],
            )


def _ensure_role_and_policy(
    iam,
    account_id: str,
    role_name: str,
    policy_name: str,
    trust_policy: dict[str, Any],
    permissions_policy: dict[str, Any],
    apply_changes: bool,
) -> str:
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
    policy_arn = f"arn:aws:iam::{account_id}:policy/{policy_name}"
    try:
        iam.get_role(RoleName=role_name)
        if apply_changes:
            iam.update_assume_role_policy(
                RoleName=role_name,
                PolicyDocument=json.dumps(trust_policy),
            )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
            raise
        if apply_changes:
            iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust_policy),
                Description=HANDLERS_OIDC_DESCRIPTION,
            )

    try:
        iam.get_policy(PolicyArn=policy_arn)
        if apply_changes:
            versions = iam.list_policy_versions(PolicyArn=policy_arn)["Versions"]
            non_default = sorted([v for v in versions if not v.get("IsDefaultVersion")], key=lambda x: x["CreateDate"])
            if len(non_default) >= 4:
                iam.delete_policy_version(PolicyArn=policy_arn, VersionId=non_default[0]["VersionId"])
            iam.create_policy_version(
                PolicyArn=policy_arn,
                PolicyDocument=json.dumps(permissions_policy),
                SetAsDefault=True,
            )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
            raise
        if apply_changes:
            iam.create_policy(
                PolicyName=policy_name,
                PolicyDocument=json.dumps(permissions_policy),
                Description=HANDLERS_OIDC_DESCRIPTION,
            )

    if apply_changes:
        iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
    return role_arn


def handlers_role_name(extension: str, github_environment: str) -> str:
    return f"GitHubActionsHandlersRole-{extension}-{github_environment}"


def handlers_policy_name(extension: str, github_environment: str) -> str:
    return f"GitHubActionsHandlersPolicy-{extension}-{github_environment}"


def run(config: HandlersBootstrapConfig) -> dict[str, Any]:
    session = _session(config.aws_profile, config.aws_region)
    sts = session.client("sts")
    iam = session.client("iam")
    account_id = sts.get_caller_identity()["Account"]
    oidc_provider_arn = _oidc_provider_arn(account_id)

    _ensure_oidc_provider(iam, oidc_provider_arn, config.apply_changes)

    ecs_bucket = config.ecs_results_bucket or f"{config.extension}-handlers-ecs-{account_id}"
    policy_replacements: dict[str, str] = {
        "EXTENSION_NAME": config.extension,
        "AWS_ACCOUNT": account_id,
        "AWS_REGION": config.aws_region,
        "ECS_RESULTS_BUCKET": ecs_bucket,
    }
    permissions_policy = _render_template(_POLICY_TEMPLATE, policy_replacements)

    role_name_prod = handlers_role_name(config.extension, "production")
    policy_name_prod = handlers_policy_name(config.extension, "production")
    trust_prod = _render_template(
        _TRUST_TEMPLATE,
        {
            "OIDC_PROVIDER_ARN": oidc_provider_arn,
            "GITHUB_REPO": config.github_repo,
            "GITHUB_ENVIRONMENT": "production",
        },
    )
    role_arn_production = _ensure_role_and_policy(
        iam=iam,
        account_id=account_id,
        role_name=role_name_prod,
        policy_name=policy_name_prod,
        trust_policy=trust_prod,
        permissions_policy=permissions_policy,
        apply_changes=config.apply_changes,
    )

    role_name_staging = ""
    role_arn_staging = ""
    policy_name_staging = ""
    if config.enable_staging_role:
        role_name_staging = handlers_role_name(config.extension, "staging")
        policy_name_staging = handlers_policy_name(config.extension, "staging")
        trust_staging = _render_template(
            _TRUST_TEMPLATE,
            {
                "OIDC_PROVIDER_ARN": oidc_provider_arn,
                "GITHUB_REPO": config.github_repo,
                "GITHUB_ENVIRONMENT": "staging",
            },
        )
        role_arn_staging = _ensure_role_and_policy(
            iam=iam,
            account_id=account_id,
            role_name=role_name_staging,
            policy_name=policy_name_staging,
            trust_policy=trust_staging,
            permissions_policy=permissions_policy,
            apply_changes=config.apply_changes,
        )

    payload: dict[str, Any] = {
        "state_version": STATE_VERSION,
        "extension": config.extension,
        "updated_at": utc_now_iso(),
        "github_repo": config.github_repo,
        "oidc_provider_arn": oidc_provider_arn,
        "ecs_results_bucket": ecs_bucket,
        "role_name_production": role_name_prod,
        "policy_name_production": policy_name_prod,
        "role_arn_production": role_arn_production,
        "role_name_staging": role_name_staging,
        "policy_name_staging": policy_name_staging,
        "role_arn_staging": role_arn_staging,
        "apply_changes": config.apply_changes,
    }
    if config.state_out_path:
        write_json(config.state_out_path, payload)
    return payload


def teardown_handlers_github_oidc(
    extension: str,
    aws_profile: str | None,
    aws_region: str,
    apply_changes: bool = True,
) -> None:
    """Remove handlers GitHub OIDC IAM roles and their customer-managed policies (not the account OIDC provider)."""
    session = _session(aws_profile, aws_region)
    sts = session.client("sts")
    iam = session.client("iam")
    account_id = sts.get_caller_identity()["Account"]

    for env_label in ("staging", "production"):
        role_name = handlers_role_name(extension, env_label)
        policy_name = handlers_policy_name(extension, env_label)
        policy_arn = f"arn:aws:iam::{account_id}:policy/{policy_name}"
        if not apply_changes:
            continue
        try:
            attached = iam.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies", [])
            for pol in attached:
                if pol["PolicyArn"] == policy_arn:
                    iam.detach_role_policy(RoleName=role_name, PolicyArn=pol["PolicyArn"])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ("NoSuchEntity",):
                raise

        try:
            for inline in iam.list_role_policies(RoleName=role_name).get("PolicyNames", []):
                iam.delete_role_policy(RoleName=role_name, PolicyName=inline)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ("NoSuchEntity",):
                raise

        try:
            iam.delete_role(RoleName=role_name)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ("NoSuchEntity",):
                raise

        try:
            versions = iam.list_policy_versions(PolicyArn=policy_arn).get("Versions", [])
            for ver in versions:
                if not ver.get("IsDefaultVersion"):
                    iam.delete_policy_version(PolicyArn=policy_arn, VersionId=ver["VersionId"])
            versions = iam.list_policy_versions(PolicyArn=policy_arn).get("Versions", [])
            for ver in versions:
                if ver.get("IsDefaultVersion"):
                    iam.delete_policy_version(PolicyArn=policy_arn, VersionId=ver["VersionId"])
            iam.delete_policy(PolicyArn=policy_arn)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ("NoSuchEntity",):
                raise
