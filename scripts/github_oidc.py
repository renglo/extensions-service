"""GitHub Actions OIDC trust helpers (legacy + immutable subject claims)."""

from __future__ import annotations

from typing import Any


def github_environment_sub_claims(
    github_repo: str,
    environment: str,
    *,
    owner_id: str | None = None,
    repo_id: str | None = None,
) -> list[str]:
    """Build ``token.actions.githubusercontent.com:sub`` values for environment-scoped roles.

    Always includes the legacy name-based claim (``repo:ORG/REPO:environment:STAGE``).
    When *owner_id* and *repo_id* are set, also includes GitHub's immutable format
    (``repo:ORG@OWNER-ID/REPO@REPO-ID:environment:STAGE``).
    """
    github_repo = github_repo.strip()
    environment = environment.strip()
    claims = [f"repo:{github_repo}:environment:{environment}"]
    owner_id = (owner_id or "").strip()
    repo_id = (repo_id or "").strip()
    if owner_id and repo_id and "/" in github_repo:
        org, repo = github_repo.split("/", 1)
        claims.append(f"repo:{org}@{owner_id}/{repo}@{repo_id}:environment:{environment}")
    return claims


def github_oidc_trust_policy(oidc_provider_arn: str, sub_claims: list[str]) -> dict[str, Any]:
    """Assume-role policy for GitHub Actions OIDC (aud + sub conditions)."""
    sub_value: str | list[str] = sub_claims if len(sub_claims) > 1 else sub_claims[0]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Federated": oidc_provider_arn},
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringEquals": {
                        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
                    },
                    "StringLike": {
                        "token.actions.githubusercontent.com:sub": sub_value,
                    },
                },
            }
        ],
    }
