"""ComputeStack: handlers stage-1 infra.

Provisions IAM, ECR, ECS cluster, task definition, S3 results bucket,
and optionally EC2 ASG + capacity provider, depending on compute_type.

compute_type values: "lambda_only" | "fargate" | "ec2"
"""

from __future__ import annotations

from typing import Any

from aws_cdk import (
    CfnCondition,
    CfnDeletionPolicy,
    CfnOutput,
    CfnParameter,
    CfnResource,
    CfnTag,
    Duration,
    Fn,
    RemovalPolicy,
)
from aws_cdk import aws_autoscaling as autoscaling
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as aws_lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

DESCRIPTION = "Reglo Deployment"
GITHUB_OIDC_PROVIDER_ARN_SUFFIX = "token.actions.githubusercontent.com"

_HANDLERS_NETWORK_MODE_CREATE = "create"
_HANDLERS_NETWORK_MODE_EXISTING = "existing"
HANDLERS_NETWORK_MODE_CREATE = _HANDLERS_NETWORK_MODE_CREATE
HANDLERS_NETWORK_MODE_EXISTING = _HANDLERS_NETWORK_MODE_EXISTING


def _apply_cfn_condition_to_construct_tree(scope: Construct, condition: CfnCondition) -> None:
    """Apply a CloudFormation condition to every L1 child under scope."""
    for construct in scope.node.find_all():
        cfn = construct.node.default_child
        if isinstance(cfn, CfnResource):
            cfn.cfn_options.condition = condition


def handlers_lambda_function_name(env_name: str) -> str:
    return f"{env_name}-handlers"


def handlers_policy_name(env_name: str) -> str:
    return f"{env_name[0].upper()}{env_name[1:]}HandlersPolicy"


def handlers_results_bucket_name(env_name: str, account: str) -> str:
    return f"{env_name}-handlers-ecs-{account}"


def _handlers_managed_policy_document(
    env_name: str,
    region: str,
    account: str,
    *,
    results_bucket_name: str,
) -> iam.PolicyDocument:
    """Matches extensions-service/scripts/setup_iam_role.sh generated policy."""
    return iam.PolicyDocument(
        statements=[
            iam.PolicyStatement(
                sid="ECSRunTask",
                actions=[
                    "ecs:RunTask",
                    "ecs:DescribeTasks",
                    "ecs:ListTasks",
                    "ecs:DescribeClusters",
                ],
                resources=[
                    f"arn:aws:ecs:{region}:{account}:cluster/{env_name}-handlers",
                    f"arn:aws:ecs:{region}:{account}:task-definition/{env_name}-handlers-ecs:*",
                    f"arn:aws:ecs:{region}:{account}:task/{env_name}-handlers/*",
                ],
            ),
            iam.PolicyStatement(
                sid="ECSPassRole",
                actions=["iam:PassRole"],
                resources=[
                    f"arn:aws:iam::{account}:role/{env_name}-handlers-ecs-execution",
                    f"arn:aws:iam::{account}:role/{env_name}-handlers-ecs-task",
                ],
                conditions={
                    "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
                },
            ),
            iam.PolicyStatement(
                sid="ECSHandshakeS3",
                actions=["s3:PutObject", "s3:GetObject"],
                resources=[f"arn:aws:s3:::{results_bucket_name}/*"],
            ),
        ]
    )


def _ecs_task_role_policy_document(
    env_name: str,
    region: str,
    account: str,
    *,
    results_bucket_name: str,
    ecr_repo_name: str,
) -> iam.PolicyDocument:
    """Matches extensions-service/utils/ecs-task-role-policy.template.json."""
    return iam.PolicyDocument(
        statements=[
            iam.PolicyStatement(
                sid="ECSHandshakeS3",
                actions=["s3:GetObject", "s3:PutObject"],
                resources=[f"arn:aws:s3:::{results_bucket_name}/*"],
            ),
            iam.PolicyStatement(
                sid="ECSRunTaskChain",
                actions=[
                    "ecs:RunTask",
                    "ecs:DescribeTasks",
                    "ecs:ListTasks",
                    "ecs:DescribeClusters",
                ],
                resources=[
                    f"arn:aws:ecs:{region}:{account}:cluster/{env_name}-handlers",
                    f"arn:aws:ecs:{region}:{account}:task-definition/{env_name}-handlers-ecs:*",
                    f"arn:aws:ecs:{region}:{account}:task/{env_name}-handlers/*",
                ],
            ),
            iam.PolicyStatement(
                sid="ECSPassRoleChain",
                actions=["iam:PassRole"],
                resources=[
                    f"arn:aws:iam::{account}:role/{env_name}-handlers-ecs-execution",
                    f"arn:aws:iam::{account}:role/{env_name}-handlers-ecs-task",
                ],
                conditions={
                    "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
                },
            ),
            iam.PolicyStatement(
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            ),
            iam.PolicyStatement(
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                ],
                resources=[f"arn:aws:ecr:{region}:{account}:repository/{ecr_repo_name}"],
            ),
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    f"arn:aws:lambda:{region}:{account}:function:{handlers_lambda_function_name(env_name)}"
                ],
            ),
        ]
    )


def _handlers_oidc_policy(
    env_name: str,
    region: str,
    account: str,
    *,
    ecs_results_bucket: str,
) -> iam.PolicyDocument:
    """Permissions for the handlers-repo GitHub Actions OIDC deploy role.

    Matches extensions-service/utils/github-handlers-actions-policy.template.json.
    """
    handlers_ecr_arn = f"arn:aws:ecr:{region}:{account}:repository/{env_name}-handlers-ecs"
    handlers_role_arn = f"arn:aws:iam::{account}:role/{env_name}-handlers-role"
    return iam.PolicyDocument(
        statements=[
            iam.PolicyStatement(
                sid="ReadIdentity",
                actions=["sts:GetCallerIdentity"],
                resources=["*"],
            ),
            iam.PolicyStatement(
                sid="EcrAuthToken",
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            ),
            iam.PolicyStatement(
                sid="EcrPushScoped",
                actions=[
                    "ecr:BatchGetImage",
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload",
                    "ecr:PutImage",
                    "ecr:DescribeRepositories",
                ],
                resources=[handlers_ecr_arn],
            ),
            iam.PolicyStatement(
                sid="EcsTaskDefinition",
                actions=[
                    "ecs:RegisterTaskDefinition",
                    "ecs:DescribeTaskDefinition",
                    "ecs:ListTaskDefinitions",
                    "ecs:DescribeClusters",
                    "ecs:ListContainerInstances",
                    "ecs:DescribeCapacityProviders",
                ],
                resources=["*"],
            ),
            iam.PolicyStatement(
                sid="LogsHandlers",
                actions=[
                    "logs:CreateLogGroup",
                    "logs:DescribeLogGroups",
                    "logs:PutRetentionPolicy",
                ],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:/ecs/{env_name}-handlers-ecs*"
                ],
            ),
            iam.PolicyStatement(
                sid="S3ResultsBucket",
                actions=[
                    "s3:GetBucketLocation",
                    "s3:ListBucket",
                    "s3:HeadBucket",
                    "s3:CreateBucket",
                    "s3:PutLifecycleConfiguration",
                    "s3:GetLifecycleConfiguration",
                    "s3:PutObject",
                    "s3:GetObject",
                    "s3:DeleteObject",
                ],
                resources=[
                    f"arn:aws:s3:::{ecs_results_bucket}",
                    f"arn:aws:s3:::{ecs_results_bucket}/*",
                ],
            ),
            iam.PolicyStatement(
                sid="PassEcsRoles",
                actions=["iam:PassRole"],
                resources=[
                    f"arn:aws:iam::{account}:role/{env_name}-handlers-ecs-execution",
                    f"arn:aws:iam::{account}:role/{env_name}-handlers-ecs-task",
                ],
                conditions={
                    "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
                },
            ),
            iam.PolicyStatement(
                sid="LambdaHandlersOptional",
                actions=[
                    "lambda:GetFunction",
                    "lambda:GetFunctionConfiguration",
                    "lambda:UpdateFunctionCode",
                    "lambda:UpdateFunctionConfiguration",
                ],
                resources=[f"arn:aws:lambda:{region}:{account}:function:{env_name}-*"],
            ),
            iam.PolicyStatement(
                actions=["iam:GetRole", "iam:PassRole"],
                resources=[handlers_role_arn],
            ),
        ]
    )

# CPU/memory defaults (Fargate-compatible)
_SIZE_DEFAULTS: dict[str, dict] = {
    "small":  {"cpu": "512",  "memory": "1024"},
    "medium": {"cpu": "1024", "memory": "4096"},
    "large":  {"cpu": "2048", "memory": "8192"},
}

# EC2 ASG min/desired/max by size
_ASG_DEFAULTS: dict[str, dict] = {
    "small":  {"min": 0, "desired": 0, "max": 1},
    "medium": {"min": 0, "desired": 1, "max": 2},
    "large":  {"min": 1, "desired": 1, "max": 4},
}


class ComputeStack(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        aws_account: str,
        aws_region: str,
        compute_type: str = "fargate",
        ec2_instance_type: str = "t3.medium",
        ec2_min_instances: int = 0,
        ec2_desired_instances: int = 1,
        ec2_max_instances: int = 2,
        task_size: str = "medium",
        network_mode: str = "awsvpc",
        github_handlers_repo: str = "",
        enable_staging: bool = True,
        tenant_policy: iam.IManagedPolicy | None = None,
        handlers_network_params: dict[str, Any] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        tt_policy = tenant_policy or iam.ManagedPolicy.from_managed_policy_name(
            self,
            "ImportedTenantPolicy",
            managed_policy_name=f"{env_name}_tt_policy",
        )
        results_bucket_name = handlers_results_bucket_name(env_name, aws_account)

        if compute_type == "lambda_only":
            self._provision_lambda_only(
                env_name,
                aws_account,
                aws_region,
                tt_policy=tt_policy,
                results_bucket_name=results_bucket_name,
            )
            self._provision_handlers_oidc(
                env_name=env_name,
                aws_account=aws_account,
                aws_region=aws_region,
                github_handlers_repo=github_handlers_repo,
                enable_staging=enable_staging,
                ecs_results_bucket=results_bucket_name,
            )
            return

        size = _SIZE_DEFAULTS.get(task_size, _SIZE_DEFAULTS["medium"])
        task_cpu = size["cpu"]
        task_memory = size["memory"]

        # --- S3 results bucket ---
        bucket_name = results_bucket_name
        results_bucket = s3.Bucket(
            self,
            "ResultsBucket",
            bucket_name=bucket_name,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpirePayloadsResults",
                    enabled=True,
                    expiration=Duration.days(1),
                    prefix="payloads/",
                ),
                s3.LifecycleRule(
                    id="ExpireResultsPrefix",
                    enabled=True,
                    expiration=Duration.days(1),
                    prefix="results/",
                ),
                s3.LifecycleRule(
                    id="ExpireStatusPrefix",
                    enabled=True,
                    expiration=Duration.days(1),
                    prefix="status/",
                ),
            ],
        )
        self.results_bucket_name = bucket_name

        # --- ECR repo for handlers image ---
        handlers_repo = ecr.Repository(
            self,
            "HandlersEcrRepo",
            repository_name=f"{env_name}-handlers-ecs",
            removal_policy=RemovalPolicy.DESTROY,
        )
        handlers_repo.add_lifecycle_rule(max_image_count=10)
        ecr_uri = f"{aws_account}.dkr.ecr.{aws_region}.amazonaws.com/{env_name}-handlers-ecs:latest"

        # --- ECS cluster (CfnCluster avoids L2 default VPC creation / AWS lookups) ---
        cluster_name = f"{env_name}-handlers"
        cluster = ecs.CfnCluster(
            self,
            "HandlersCluster",
            cluster_name=cluster_name,
        )

        # --- IAM: execution role ---
        execution_role = iam.Role(
            self,
            "EcsExecutionRole",
            role_name=f"{env_name}-handlers-ecs-execution",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                )
            ],
            description=DESCRIPTION,
        )

        # --- IAM: task role (inline policy + HandlersPolicy + tt_policy) ---
        task_role = iam.Role(
            self,
            "EcsTaskRole",
            role_name=f"{env_name}-handlers-ecs-task",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description=DESCRIPTION,
        )
        self.handlers_ecs_task_role = task_role

        # --- IAM: handlers Lambda execution role ---
        handlers_policy = iam.ManagedPolicy(
            self,
            "HandlersPolicy",
            managed_policy_name=handlers_policy_name(env_name),
            document=_handlers_managed_policy_document(
                env_name,
                aws_region,
                aws_account,
                results_bucket_name=bucket_name,
            ),
        )
        task_role.add_managed_policy(handlers_policy)
        task_role.add_managed_policy(tt_policy)
        task_role.attach_inline_policy(
            iam.Policy(
                self,
                "EcsTaskRoleInlinePolicy",
                policy_name="ecs-handlers-s3-ecr",
                document=_ecs_task_role_policy_document(
                    env_name,
                    aws_region,
                    aws_account,
                    results_bucket_name=bucket_name,
                    ecr_repo_name=f"{env_name}-handlers-ecs",
                ),
            )
        )

        handlers_lambda_role = iam.Role(
            self,
            "HandlersLambdaRole",
            role_name=f"{env_name}-handlers-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
                handlers_policy,
                tt_policy,
            ],
            description=DESCRIPTION,
        )
        self.handlers_lambda_role = handlers_lambda_role

        handlers_lambda_log_group = logs.LogGroup(
            self,
            "HandlersLambdaLogGroup",
            log_group_name=f"/aws/lambda/{handlers_lambda_function_name(env_name)}",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Handlers Lambda (seed — pipeline updates code after first deploy) ---
        handlers_fn = aws_lambda_.CfnFunction(
            self,
            "HandlersLambda",
            function_name=handlers_lambda_function_name(env_name),
            role=handlers_lambda_role.role_arn,
            runtime="python3.12",
            handler="index.handler",
            code=aws_lambda_.CfnFunction.CodeProperty(
                zip_file=(
                    "def handler(event, context):\n"
                    "    return {'statusCode': 200, 'body': 'seed'}\n"
                ),
            ),
            timeout=900,
            memory_size=512,
            description=DESCRIPTION,
        )
        handlers_fn.add_dependency(handlers_lambda_role.node.default_child)  # type: ignore[arg-type]
        handlers_fn.cfn_options.deletion_policy = CfnDeletionPolicy.DELETE
        handlers_fn.cfn_options.update_replace_policy = CfnDeletionPolicy.DELETE

        # --- CloudWatch log group (ECS tasks) ---
        task_family = f"{env_name}-handlers-ecs"
        log_group = logs.LogGroup(
            self,
            "HandlersLogGroup",
            log_group_name=f"/ecs/{task_family}",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- ECS task definition ---
        task_def = self._make_task_definition(
            env_name=env_name,
            task_family=task_family,
            ecr_uri=ecr_uri,
            execution_role=execution_role,
            task_role=task_role,
            log_group=log_group,
            aws_region=aws_region,
            task_cpu=task_cpu,
            task_memory=task_memory,
            compute_type=compute_type,
            network_mode=network_mode,
        )

        # --- EC2 capacity (only when compute_type == "ec2") ---
        ec2_network_outputs: dict[str, str] = {}
        if compute_type == "ec2":
            if handlers_network_params is None:
                raise ValueError("handlers_network_params is required when compute_type is ec2")
            ec2_network_outputs = self._provision_ec2_capacity(
                env_name=env_name,
                cluster_name=cluster_name,
                instance_type=ec2_instance_type,
                ec2_min=ec2_min_instances,
                ec2_desired=ec2_desired_instances,
                ec2_max=ec2_max_instances,
                handlers_network_params=handlers_network_params,
            )

        # --- Outputs ---
        CfnOutput(self, "HandlersEcrRepoName", value=handlers_repo.repository_name)
        CfnOutput(self, "HandlersEcrRepoUri", value=handlers_repo.repository_uri)
        CfnOutput(self, "HandlersEcsClusterName", value=cluster_name)
        CfnOutput(self, "HandlersResultsBucketName", value=results_bucket.bucket_name)
        CfnOutput(self, "HandlersExecutionRoleArn", value=execution_role.role_arn)
        CfnOutput(self, "HandlersTaskRoleArn", value=task_role.role_arn)
        CfnOutput(self, "HandlersLambdaRoleArn", value=handlers_lambda_role.role_arn)
        CfnOutput(self, "HandlersLambdaLogGroupName", value=handlers_lambda_log_group.log_group_name)
        CfnOutput(self, "HandlersLambdaFunctionName", value=handlers_fn.function_name)  # type: ignore[arg-type]
        CfnOutput(self, "HandlersTaskFamily", value=task_family)

        self.stable_outputs = {
            "HandlersEcrRepoName": handlers_repo.repository_name,
            "HandlersEcrRepoUri": handlers_repo.repository_uri,
            "HandlersEcsClusterName": cluster_name,
            "HandlersResultsBucketName": results_bucket.bucket_name,
            "HandlersExecutionRoleArn": execution_role.role_arn,
            "HandlersTaskRoleArn": task_role.role_arn,
            "HandlersLambdaRoleArn": handlers_lambda_role.role_arn,
            "HandlersLambdaLogGroupName": handlers_lambda_log_group.log_group_name,
            "HandlersLambdaFunctionName": handlers_fn.function_name,  # type: ignore[arg-type]
            "HandlersTaskFamily": task_family,
            **ec2_network_outputs,
        }

        self._provision_handlers_oidc(
            env_name=env_name,
            aws_account=aws_account,
            aws_region=aws_region,
            github_handlers_repo=github_handlers_repo,
            enable_staging=enable_staging,
            ecs_results_bucket=bucket_name,
        )

    def _provision_handlers_oidc(
        self,
        *,
        env_name: str,
        aws_account: str,
        aws_region: str,
        github_handlers_repo: str,
        enable_staging: bool,
        ecs_results_bucket: str,
    ) -> None:
        if not github_handlers_repo.strip():
            return

        oidc_provider = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(
            self,
            "HandlersGitHubOidcProvider",
            open_id_connect_provider_arn=(
                f"arn:aws:iam::{aws_account}:oidc-provider/{GITHUB_OIDC_PROVIDER_ARN_SUFFIX}"
            ),
        )
        policy_doc = _handlers_oidc_policy(
            env_name,
            aws_region,
            aws_account,
            ecs_results_bucket=ecs_results_bucket,
        )

        def _handlers_oidc_role(stage: str) -> iam.Role:
            return iam.Role(
                self,
                f"HandlersOidcDeployRole{stage.capitalize()}",
                role_name=f"GitHubActionsHandlersRole-{env_name}-{stage}",
                assumed_by=iam.WebIdentityPrincipal(
                    oidc_provider.open_id_connect_provider_arn,
                    conditions={
                        "StringEquals": {
                            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
                        },
                        "StringLike": {
                            "token.actions.githubusercontent.com:sub": (
                                f"repo:{github_handlers_repo}:environment:{stage}"
                            )
                        },
                    },
                ),
                inline_policies={
                    f"GitHubActionsHandlersPolicy-{env_name}-{stage}": policy_doc
                },
                description=DESCRIPTION,
            )

        prod_role = _handlers_oidc_role("production")
        CfnOutput(self, "HandlersOidcDeployRoleArnProduction", value=prod_role.role_arn)
        oidc_outputs = {"HandlersOidcDeployRoleArnProduction": prod_role.role_arn}
        if enable_staging:
            staging_role = _handlers_oidc_role("staging")
            CfnOutput(self, "HandlersOidcDeployRoleArnStaging", value=staging_role.role_arn)
            oidc_outputs["HandlersOidcDeployRoleArnStaging"] = staging_role.role_arn
        existing = getattr(self, "stable_outputs", None) or {}
        self.stable_outputs = {**existing, **oidc_outputs}

    def _provision_lambda_only(
        self,
        env_name: str,
        aws_account: str,
        aws_region: str,
        *,
        tt_policy: iam.IManagedPolicy,
        results_bucket_name: str,
    ) -> None:
        """Lambda-only mode: handlers IAM role + Lambda seed + log group without ECS infra."""
        handlers_policy = iam.ManagedPolicy(
            self,
            "HandlersPolicy",
            managed_policy_name=handlers_policy_name(env_name),
            document=_handlers_managed_policy_document(
                env_name,
                aws_region,
                aws_account,
                results_bucket_name=results_bucket_name,
            ),
        )
        handlers_lambda_role = iam.Role(
            self,
            "HandlersLambdaRole",
            role_name=f"{env_name}-handlers-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
                handlers_policy,
                tt_policy,
            ],
            description=DESCRIPTION,
        )
        self.handlers_lambda_role = handlers_lambda_role
        handlers_lambda_log_group = logs.LogGroup(
            self,
            "HandlersLambdaLogGroup",
            log_group_name=f"/aws/lambda/{handlers_lambda_function_name(env_name)}",
            removal_policy=RemovalPolicy.DESTROY,
        )
        handlers_fn = aws_lambda_.CfnFunction(
            self,
            "HandlersLambda",
            function_name=handlers_lambda_function_name(env_name),
            role=handlers_lambda_role.role_arn,
            runtime="python3.12",
            handler="index.handler",
            code=aws_lambda_.CfnFunction.CodeProperty(
                zip_file=(
                    "def handler(event, context):\n"
                    "    return {'statusCode': 200, 'body': 'seed'}\n"
                ),
            ),
            timeout=900,
            memory_size=512,
            description=DESCRIPTION,
        )
        handlers_fn.add_dependency(handlers_lambda_role.node.default_child)  # type: ignore[arg-type]
        handlers_fn.cfn_options.deletion_policy = CfnDeletionPolicy.DELETE
        handlers_fn.cfn_options.update_replace_policy = CfnDeletionPolicy.DELETE
        CfnOutput(self, "HandlersLambdaRoleArn", value=handlers_lambda_role.role_arn)
        CfnOutput(self, "HandlersLambdaLogGroupName", value=handlers_lambda_log_group.log_group_name)
        CfnOutput(self, "HandlersLambdaFunctionName", value=handlers_fn.function_name)  # type: ignore[arg-type]
        CfnOutput(self, "ComputeType", value="lambda_only")
        self.stable_outputs = {
            "HandlersLambdaRoleArn": handlers_lambda_role.role_arn,
            "HandlersLambdaLogGroupName": handlers_lambda_log_group.log_group_name,
            "HandlersLambdaFunctionName": handlers_fn.function_name,  # type: ignore[arg-type]
        }

    def _make_task_definition(
        self,
        *,
        env_name: str,
        task_family: str,
        ecr_uri: str,
        execution_role: iam.Role,
        task_role: iam.Role,
        log_group: logs.LogGroup,
        aws_region: str,
        task_cpu: str,
        task_memory: str,
        compute_type: str,
        network_mode: str,
    ) -> ecs.TaskDefinition:
        if compute_type == "fargate":
            cfn_network_mode = ecs.NetworkMode.AWS_VPC
            compat = ecs.Compatibility.FARGATE
        else:
            mode_map = {
                "awsvpc": ecs.NetworkMode.AWS_VPC,
                "bridge": ecs.NetworkMode.BRIDGE,
                "host": ecs.NetworkMode.HOST,
            }
            cfn_network_mode = mode_map.get(network_mode, ecs.NetworkMode.BRIDGE)
            compat = ecs.Compatibility.EC2

        task_def = ecs.TaskDefinition(
            self,
            "HandlersTaskDef",
            family=task_family,
            execution_role=execution_role,
            task_role=task_role,
            network_mode=cfn_network_mode,
            compatibility=compat,
            cpu=task_cpu,
            memory_mib=task_memory,
        )
        task_def.add_container(
            "handler",
            image=ecs.ContainerImage.from_registry(ecr_uri),
            essential=True,
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="ecs",
                log_group=log_group,
            ),
        )
        return task_def

    def _provision_ec2_capacity(
        self,
        *,
        env_name: str,
        cluster_name: str,
        instance_type: str,
        ec2_min: int,
        ec2_desired: int,
        ec2_max: int,
        handlers_network_params: dict[str, Any],
    ) -> dict[str, str]:
        """Add EC2 ASG + ECS capacity provider to the cluster.

        Network layout is selected at deploy time via stack-level CloudFormation
        parameters (see stack_b.py). create provisions dedicated VPC/subnets;
        existing uses customer VPC/subnets and still creates a dedicated SG.
        """
        create_dedicated_network = handlers_network_params["create_dedicated_network"]
        use_existing_network = handlers_network_params["use_existing_network"]
        existing_vpc_id = handlers_network_params["existing_vpc_id"]
        existing_subnet_ids = handlers_network_params["existing_subnet_ids"]

        ecs_ami_param = CfnParameter(
            self,
            "EcsOptimizedAmi",
            type="AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>",
            default="/aws/service/ecs/optimized-ami/amazon-linux-2/recommended/image_id",
        )

        vpc = ec2.Vpc(
            self,
            "HandlersComputeVpc",
            nat_gateways=0,
            max_azs=2,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="handlers-public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                )
            ],
        )
        _apply_cfn_condition_to_construct_tree(vpc, create_dedicated_network)

        dedicated_subnet_ids = Fn.join(",", [subnet.subnet_id for subnet in vpc.public_subnets])
        vpc_id_for_resources = Fn.condition_if(
            create_dedicated_network.logical_id,
            vpc.vpc_id,
            existing_vpc_id.value_as_string,
        )
        dedicated_subnet_id_list = [subnet.subnet_id for subnet in vpc.public_subnets]

        instance_role = iam.Role(
            self,
            "HandlersAsgInstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonEC2ContainerServiceforEC2Role"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
            ],
        )
        instance_profile = iam.CfnInstanceProfile(
            self,
            "HandlersAsgInstanceProfile",
            roles=[instance_role.role_name],
        )

        security_group_create = ec2.CfnSecurityGroup(
            self,
            "HandlersAsgInstanceSecurityGroupCreate",
            group_description=f"{env_name} handlers ECS EC2 instances",
            vpc_id=vpc.vpc_id,
        )
        security_group_create.cfn_options.condition = create_dedicated_network

        security_group_existing = ec2.CfnSecurityGroup(
            self,
            "HandlersAsgInstanceSecurityGroupExisting",
            group_description=f"{env_name} handlers ECS EC2 instances",
            vpc_id=existing_vpc_id.value_as_string,
        )
        security_group_existing.cfn_options.condition = use_existing_network

        security_group_id = Fn.condition_if(
            create_dedicated_network.logical_id,
            security_group_create.ref,
            security_group_existing.ref,
        )

        asg_name = f"{env_name}-handlers-ecs-asg"
        cp_name = f"{env_name}-handlers-ecs-cp"

        # ECS-optimized AMI registers with the "default" cluster unless ECS_CLUSTER is set.
        ecs_user_data = Fn.base64(
            f"#!/bin/bash\necho ECS_CLUSTER={cluster_name} >> /etc/ecs/ecs.config\n"
        )

        def _launch_template_data(security_group_ref: str) -> ec2.CfnLaunchTemplate.LaunchTemplateDataProperty:
            return ec2.CfnLaunchTemplate.LaunchTemplateDataProperty(
                image_id=ecs_ami_param.value_as_string,
                instance_type=instance_type,
                iam_instance_profile=ec2.CfnLaunchTemplate.IamInstanceProfileProperty(
                    arn=instance_profile.attr_arn,
                ),
                security_group_ids=[security_group_ref],
                user_data=ecs_user_data,
                tag_specifications=[
                    ec2.CfnLaunchTemplate.TagSpecificationProperty(
                        resource_type="instance",
                        tags=[CfnTag(key="Name", value=asg_name)],
                    ),
                ],
            )

        launch_template_create = ec2.CfnLaunchTemplate(
            self,
            "HandlersAsgLaunchTemplateCreate",
            launch_template_name=f"{env_name}-handlers-ecs-lt",
            launch_template_data=_launch_template_data(security_group_create.ref),
        )
        launch_template_create.cfn_options.condition = create_dedicated_network

        launch_template_existing = ec2.CfnLaunchTemplate(
            self,
            "HandlersAsgLaunchTemplateExisting",
            launch_template_name=f"{env_name}-handlers-ecs-lt",
            launch_template_data=_launch_template_data(security_group_existing.ref),
        )
        launch_template_existing.cfn_options.condition = use_existing_network

        cfn_asg_create = autoscaling.CfnAutoScalingGroup(
            self,
            "HandlersAsgCreate",
            auto_scaling_group_name=asg_name,
            min_size=str(ec2_min),
            max_size=str(ec2_max),
            desired_capacity=str(ec2_desired),
            launch_template=autoscaling.CfnAutoScalingGroup.LaunchTemplateSpecificationProperty(
                launch_template_id=launch_template_create.ref,
                version=launch_template_create.attr_latest_version_number,
            ),
            vpc_zone_identifier=dedicated_subnet_id_list,
            new_instances_protected_from_scale_in=True,
            tags=[
                autoscaling.CfnAutoScalingGroup.TagPropertyProperty(
                    key="Name",
                    value=asg_name,
                    propagate_at_launch=False,
                )
            ],
        )
        cfn_asg_create.cfn_options.condition = create_dedicated_network

        cfn_asg_existing = autoscaling.CfnAutoScalingGroup(
            self,
            "HandlersAsgExisting",
            auto_scaling_group_name=asg_name,
            min_size=str(ec2_min),
            max_size=str(ec2_max),
            desired_capacity=str(ec2_desired),
            launch_template=autoscaling.CfnAutoScalingGroup.LaunchTemplateSpecificationProperty(
                launch_template_id=launch_template_existing.ref,
                version=launch_template_existing.attr_latest_version_number,
            ),
            vpc_zone_identifier=existing_subnet_ids.value_as_list,
            new_instances_protected_from_scale_in=True,
            tags=[
                autoscaling.CfnAutoScalingGroup.TagPropertyProperty(
                    key="Name",
                    value=asg_name,
                    propagate_at_launch=False,
                )
            ],
        )
        cfn_asg_existing.cfn_options.condition = use_existing_network

        capacity_provider_create = ecs.CfnCapacityProvider(
            self,
            "HandlersCapacityProviderCreate",
            name=cp_name,
            auto_scaling_group_provider=ecs.CfnCapacityProvider.AutoScalingGroupProviderProperty(
                auto_scaling_group_arn=cfn_asg_create.ref,
                managed_scaling=ecs.CfnCapacityProvider.ManagedScalingProperty(
                    status="ENABLED",
                    target_capacity=100,
                ),
                managed_termination_protection="ENABLED",
            ),
        )
        capacity_provider_create.cfn_options.condition = create_dedicated_network

        capacity_provider_existing = ecs.CfnCapacityProvider(
            self,
            "HandlersCapacityProviderExisting",
            name=cp_name,
            auto_scaling_group_provider=ecs.CfnCapacityProvider.AutoScalingGroupProviderProperty(
                auto_scaling_group_arn=cfn_asg_existing.ref,
                managed_scaling=ecs.CfnCapacityProvider.ManagedScalingProperty(
                    status="ENABLED",
                    target_capacity=100,
                ),
                managed_termination_protection="ENABLED",
            ),
        )
        capacity_provider_existing.cfn_options.condition = use_existing_network

        cluster_cp_associations_create = ecs.CfnClusterCapacityProviderAssociations(
            self,
            "HandlersClusterCapacityProvidersCreate",
            cluster=cluster_name,
            capacity_providers=[cp_name],
            default_capacity_provider_strategy=[
                ecs.CfnClusterCapacityProviderAssociations.CapacityProviderStrategyProperty(
                    capacity_provider=cp_name,
                    weight=1,
                    base=0,
                )
            ],
        )
        cluster_cp_associations_create.cfn_options.condition = create_dedicated_network
        cluster_cp_associations_create.add_dependency(capacity_provider_create)

        cluster_cp_associations_existing = ecs.CfnClusterCapacityProviderAssociations(
            self,
            "HandlersClusterCapacityProvidersExisting",
            cluster=cluster_name,
            capacity_providers=[cp_name],
            default_capacity_provider_strategy=[
                ecs.CfnClusterCapacityProviderAssociations.CapacityProviderStrategyProperty(
                    capacity_provider=cp_name,
                    weight=1,
                    base=0,
                )
            ],
        )
        cluster_cp_associations_existing.cfn_options.condition = use_existing_network
        cluster_cp_associations_existing.add_dependency(capacity_provider_existing)

        cfn_asg_create.node.add_dependency(launch_template_create)
        cfn_asg_existing.node.add_dependency(launch_template_existing)
        capacity_provider_create.node.add_dependency(cfn_asg_create)
        capacity_provider_existing.node.add_dependency(cfn_asg_existing)

        return {
            "HandlersComputeVpcId": vpc_id_for_resources,
            "HandlersComputeSubnetIds": Fn.condition_if(
                create_dedicated_network.logical_id,
                dedicated_subnet_ids,
                Fn.join(",", existing_subnet_ids.value_as_list),
            ),
            "HandlersComputeSecurityGroupId": security_group_id,
        }
