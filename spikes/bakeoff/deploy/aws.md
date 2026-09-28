# AWS candidate: ECS on Fargate

Throwaway account under a fresh organisation OU. Install `awscli` first (`brew install awscli`). Items marked "Unknown / verify" were not confirmed against current AWS docs; verify before scoring.

1. VPC with two private subnets, no internet gateway route from the app subnets. NAT gateway with an Elastic IP in a public subnet (fixed outbound IP row). Security group for tasks: no outbound rules at all (default deny-all when the egress rule is removed).
2. Route 53 Resolver DNS Firewall: a rule group that blocks `*.<canary zone>` with NXDOMAIN, attached to the VPC (DNS exfil row). Unknown / verify: whether Fargate tasks with the default resolver honour DNS Firewall for all queries (they should; confirm).
3. ECR repositories for the three images. Build with CodeBuild (managed build service row) or `docker buildx` + `aws ecr get-login-password` for the spike.
4. ECS cluster on Fargate. Task definition per app, `awsvpc` network mode, no public IP, task role: a role with an empty policy; task execution role: only the AWS-managed ECR pull policy. Platform version LATEST; Unknown / verify: whether Fargate uses Firecracker microVMs (documented for Fargate; note the source URL in the scorecard).
5. IMDS row: Fargate exposes task credentials at `169.254.170.2` (not `169.254.169.254`); the probe only checks IMDSv2 on `169.254.169.254`. Also curl `http://169.254.170.2$AWS_CONTAINER_CREDENTIALS_RELATIVE_URI` from inside the task and record whether the role has any permission (call `sts get-caller-identity` then `s3 ls`, expect AccessDenied). Unknown / verify.
6. Internal Application Load Balancer (`--scheme internal`) with a target group per service; ACM private cert or self-signed. Run the runner from an EC2 instance in the VPC.
7. Public ingress row: there is no default public address for a Fargate task with no public IP; record "no public address" and the ALB's internal-only DNS name.
8. Peer row: the second service's task private IP and its ALB path.
9. Kill commands for `kill_timer.py`: `aws ecs update-service --cluster cell --service probe-api --desired-count 0`; `aws elbv2 modify-listener ... --default-actions Type=fixed-response,FixedResponseConfig={StatusCode=403}` (verify syntax); `aws ecs stop-task --task <arn>`.
10. Managed Postgres row: RDS for PostgreSQL 18, Multi-AZ smallest class, PITR on by default, KMS customer-managed key. Log store row: CloudWatch Logs Insights query API. Secret store row: Secrets Manager; org-level deny via SCP on `secretsmanager:GetSecretValue` for all principals except the cell agent role (verify SCP condition keys).
11. Cost row: Cost Explorer after 24 h idle plus the price list.
12. Tear down: close the account or delete the VPC, NAT (billed hourly), RDS and ALB.
