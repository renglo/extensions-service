from __future__ import annotations

from pathlib import Path

from deploy_input import (
    LAMBDA_DIRECT_UPLOAD_MAX_BYTES,
    get_s3_bucket_name_from_payload,
)


def test_lambda_direct_upload_limit_is_50mb() -> None:
    assert LAMBDA_DIRECT_UPLOAD_MAX_BYTES == 50 * 1024 * 1024


def test_get_s3_bucket_name_from_payload() -> None:
    payload = {
        "GITHUB_REPOSITORY": "org/repo",
        "ENVIRONMENT": "production",
        "VARS": {"S3_BUCKET_NAME": "tenant-deploy-bucket"},
    }
    assert get_s3_bucket_name_from_payload(payload) == "tenant-deploy-bucket"


def test_get_s3_bucket_name_missing_or_blank(tmp_path: Path) -> None:
    payload = {
        "GITHUB_REPOSITORY": "org/repo",
        "ENVIRONMENT": "production",
        "VARS": {},
    }
    assert get_s3_bucket_name_from_payload(payload) is None
    payload["VARS"]["S3_BUCKET_NAME"] = "  "
    assert get_s3_bucket_name_from_payload(payload) is None
