# Client AWS Role Setup

This guide covers the minimum cross-account setup for connecting a client AWS account without sharing long-lived access keys.

## What belongs where

- Our AWS account contains the role that initiates access.
- The client AWS account contains the role that trusts our role.
- The client sends us their role ARN after creation.
- We send the client our role ARN so they can place it into `Principal`.

## Step 1: Create or identify our source role

Create this role in our AWS account, or use an existing role that our operator will assume locally.

Example ARN:

```text
arn:aws:iam::<OUR_ACCOUNT_ID>:role/<OUR_ROLE_NAME>
```

Optional inline policy for our side if you want to limit what this role can assume:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:aws:iam::<CLIENT_ACCOUNT_ID>:role/<CLIENT_ROLE_NAME>"
    }
  ]
}
```

## Step 2: Send our role ARN to the client

Send the client this value:

```text
arn:aws:iam::<OUR_ACCOUNT_ID>:role/<OUR_ROLE_NAME>
```

The client must place this ARN into the `Principal` field of the trust policy on their side.

## Step 3: Client creates a trusted role

The client creates an IAM role in their AWS account with permissions appropriate for the job.

Recommended split:

- `ReadOnly` role for discovery and audit
- `Deploy` role for approved write operations

## Step 4: Client trust policy

Basic version:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::<OUR_ACCOUNT_ID>:role/<OUR_ROLE_NAME>"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

Recommended version with `ExternalId`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::<OUR_ACCOUNT_ID>:role/<OUR_ROLE_NAME>"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "<EXTERNAL_ID>"
        }
      }
    }
  ]
}
```

## Step 5: What the client sends back

The client should send us:

- `Role ARN`
- `ExternalId` if they configured one
- AWS region or list of regions
- whether access is `read-only` or `deploy`

Example client role ARN:

```text
arn:aws:iam::<CLIENT_ACCOUNT_ID>:role/<CLIENT_ROLE_NAME>
```

## Step 6: What we place in our config

Our code already supports role-based access and optional external IDs.

Example:

```json
{
  "source_role_arn": "arn:aws:iam::<CLIENT_ACCOUNT_ID>:role/<CLIENT_READ_ROLE>",
  "target_role_arn": "arn:aws:iam::<CLIENT_ACCOUNT_ID>:role/<CLIENT_DEPLOY_ROLE>",
  "overrides": {
    "source_region": "us-east-1",
    "target_region": "us-east-2",
    "source_external_id": "<EXTERNAL_ID>",
    "target_external_id": "<EXTERNAL_ID>"
  }
}
```

If only one client role exists, use the same ARN only in the flow that needs it.

For multi-region account migration, you can also provide:

```json
{
  "overrides": {
    "source_regions": ["us-east-1", "us-west-2"]
  }
}
```

Then run:

```text
python executor/scripts/migrate_account.py --source-env <SOURCE_ENV> --target-env <TARGET_ENV> --config <CONFIG_PATH> --source-role-arn <SOURCE_ROLE_ARN> --target-role-arn <TARGET_ROLE_ARN>
```

## Minimal message to send the client

```text
Please create an IAM Role in your AWS account for cross-account access via STS AssumeRole.
Use this principal in the trust policy:
arn:aws:iam::<OUR_ACCOUNT_ID>:role/<OUR_ROLE_NAME>

If preferred, add an ExternalId condition for additional protection.
Please send us the resulting Role ARN, ExternalId if used, and the AWS regions we should access.
```

## Important limitation

Without valid AWS credentials in the local CLI session, we can prepare policies and commands but cannot create or inspect IAM roles in AWS directly.
