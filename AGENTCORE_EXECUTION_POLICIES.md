# AgentCore Runtime Execution Policies

This document defines the IAM execution policies required for:
- MCP runtime (`bcbs_dev_convai_mcp-Vt2A72DKeq`)
- Orchestrator runtime (`bcbs_dev_convai_orchestrator-GEkw1YGeCY`)
- Account-management runtime (`bcbs_dev_convai_acct_mgnt-8xi3SACIES`)

It also includes policy cleanup guidance to remove temporary broad access after testing.

## Runtime and Role Mapping

Current runtime roles in this environment:
- MCP runtime role: `arn:aws:iam::834458830002:role/service-role/AmazonBedrockAgentCoreRuntimeDefaultServiceRole-zix3c`
- Orchestrator runtime role: `arn:aws:iam::834458830002:role/service-role/AmazonBedrockAgentCoreRuntimeDefaultServiceRole-kugos`
- Account-management runtime role: confirm with `get-agent-runtime` if it differs from orchestrator role.

## Required Access by Component

### 1) MCP Runtime (calls orchestrator invocations URL)

Purpose:
- MCP tool `orchestrator_invoke` sends a SigV4 signed request to orchestrator runtime `/invocations`.

Minimum cross-runtime permission:
- `bedrock-agentcore:InvokeRuntime` on orchestrator runtime ARN.

Policy statement:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowInvokeOrchestratorRuntime",
      "Effect": "Allow",
      "Action": "bedrock-agentcore:InvokeRuntime",
      "Resource": "arn:aws:bedrock-agentcore:us-east-1:834458830002:runtime/bcbs_dev_convai_orchestrator-GEkw1YGeCY"
    }
  ]
}
```

### 2) Orchestrator Runtime (calls acct-mgmt invocations URL)

Purpose:
- Orchestrator service delegates account actions by sending SigV4 signed requests to acct-mgmt runtime `/invocations`.

Minimum cross-runtime permission:
- `bedrock-agentcore:InvokeRuntime` on acct-mgmt runtime ARN.

Policy statement:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowInvokeAcctRuntime",
      "Effect": "Allow",
      "Action": "bedrock-agentcore:InvokeRuntime",
      "Resource": "arn:aws:bedrock-agentcore:us-east-1:834458830002:runtime/bcbs_dev_convai_acct_mgnt-8xi3SACIES"
    }
  ]
}
```

### 3) Account-management Runtime

Purpose:
- Serves `/invocations` and runs Strands model/tool logic.
- Does not call other runtimes in current design.

Required permissions:
- Standard AgentCore runtime execution permissions (ECR pull, CloudWatch logs, Bedrock model invoke, optional Secrets Manager if enabled).
- No cross-runtime `InvokeRuntime` permission needed unless this runtime starts invoking others.

## Baseline Runtime Permissions (all runtimes)

Each runtime role should include baseline execution access typically provisioned by AgentCore runtime execution policy:
- ECR image pull:
  - `ecr:GetAuthorizationToken`
  - `ecr:BatchGetImage`
  - `ecr:GetDownloadUrlForLayer`
- Logs:
  - `logs:CreateLogGroup`
  - `logs:CreateLogStream`
  - `logs:PutLogEvents`
  - `logs:DescribeLogGroups`
  - `logs:DescribeLogStreams`
- Bedrock model invoke (if runtime uses LLM):
  - `bedrock:InvokeModel`
  - `bedrock:InvokeModelWithResponseStream`
- Workload identity token access (AgentCore runtime internals):
  - `bedrock-agentcore:GetWorkloadAccessToken`
  - `bedrock-agentcore:GetWorkloadAccessTokenForJWT`
  - `bedrock-agentcore:GetWorkloadAccessTokenForUserId`
- Optional (if `AWS_SECRETSMANAGER_ENABLED=true`):
  - `secretsmanager:GetSecretValue` on your secret ARN

## Environment Variable Dependencies

Correct runtime URLs are required in addition to IAM policy.

MCP runtime:
- `SERVICE_ORCHESTRATOR_URL=https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/<encoded-orchestrator-arn>/invocations?qualifier=<orchestrator-endpoint>`

Orchestrator runtime:
- `SERVICE_ACCOUNT_AGENT_INVOCATIONS_URL=https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/<encoded-acct-arn>/invocations?qualifier=<acct-endpoint>`

## Apply Policies (CLI examples)

Attach MCP cross-runtime policy inline:

```powershell
aws iam put-role-policy \
  --role-name "AmazonBedrockAgentCoreRuntimeDefaultServiceRole-zix3c" \
  --policy-name "AllowInvokeOrchestratorRuntime" \
  --policy-document file://allow-invoke-orchestrator.json
```

Attach orchestrator cross-runtime policy inline:

```powershell
aws iam put-role-policy \
  --role-name "AmazonBedrockAgentCoreRuntimeDefaultServiceRole-kugos" \
  --policy-name "AllowInvokeAcctRuntime" \
  --policy-document file://allow-invoke-acct.json
```

## Remove Temporary Broad Access (important)

If temporary unblock policy was added:
- `TempAgentCoreFullAccess` (`bedrock-agentcore:*` on `*`)

Remove it from both roles:

```powershell
aws iam delete-role-policy --role-name "AmazonBedrockAgentCoreRuntimeDefaultServiceRole-zix3c" --policy-name "TempAgentCoreFullAccess"
aws iam delete-role-policy --role-name "AmazonBedrockAgentCoreRuntimeDefaultServiceRole-kugos" --policy-name "TempAgentCoreFullAccess"
```

## Verification Checklist

1. Verify inline policies exist:

```powershell
aws iam get-role-policy --role-name "AmazonBedrockAgentCoreRuntimeDefaultServiceRole-zix3c" --policy-name "AllowInvokeOrchestratorRuntime"
aws iam get-role-policy --role-name "AmazonBedrockAgentCoreRuntimeDefaultServiceRole-kugos" --policy-name "AllowInvokeAcctRuntime"
```

2. Verify runtime env vars are correct:

```powershell
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "bcbs_dev_convai_mcp-Vt2A72DKeq" --region us-east-1
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "bcbs_dev_convai_orchestrator-GEkw1YGeCY" --region us-east-1
```

3. Run end-to-end test:

```powershell
python tests\request_gateway.py --call-sample --sample-intent Account_Unlock
python tests\request_gateway.py --call-sample --sample-intent Account_PW_Reset
```

## Notes

- For PUBLIC network mode, runtime-to-runtime calls still require valid SigV4 and IAM permissions.
- Keep policies least-privilege by scoping `InvokeRuntime` to specific runtime ARNs.
- If runtime IDs or role names change, update ARNs and role names accordingly.
