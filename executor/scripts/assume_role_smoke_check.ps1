if (-not $env:AWS_ACCESS_KEY_ID -or -not $env:AWS_SECRET_ACCESS_KEY) {
    Write-Error "Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY before running this script."
    exit 1
}

$env:AWS_DEFAULT_REGION = 'us-east-1'
$env:AWS_REGION = 'us-east-1'

python executor/scripts/assume_role_smoke_check.py --role-arn arn:aws:iam::198758256518:role/aws-dev-agent-transfer-role --region us-east-1
