# AWS Cost Report

Generated: 2026-03-21T20:20:48.655292+00:00

## Summary

Month-to-date AWS spend is -0.0 USD, with a forecast of 1.05 USD by month end.

## Service Breakdown

- AWS CloudShell: 0.0 USD
- AWS Data Transfer: -0.0 USD
- AWS Glue: 0.0 USD
- AWS Key Management Service: 0.0 USD
- AWS Lambda: 0.0 USD

## Region Breakdown

- NoRegion: 0.0 USD
- eu-north-1: 0.0 USD
- global: 0.0 USD
- us-east-1: -0.0 USD
- us-east-2: -0.0 USD

## Recommended Actions

- [high] Pause failing ECS service until its image or task definition is fixed
  Resource: arn:aws:ecs:us-east-1:027087672282:service/carefree-crocodile-bf3zi5/test_def-service-vnwns50w
  Why: test_def-service-vnwns50w keeps retrying failed task launches (5 recent failure events) while desiredCount remains > 0.
  Action: Set desiredCount to 0 or disable the service until the container image and startup path are healthy.
- [high] Pause failing ECS service until its image or task definition is fixed
  Resource: arn:aws:ecs:us-east-1:027087672282:service/carefree-crocodile-bf3zi5/test_def-service-64zgxsih
  Why: test_def-service-64zgxsih keeps retrying failed task launches (5 recent failure events) while desiredCount remains > 0.
  Action: Set desiredCount to 0 or disable the service until the container image and startup path are healthy.
- [medium] Move non-production ECS service to Fargate Spot when interruption is acceptable
  Resource: arn:aws:ecs:us-east-1:027087672282:service/carefree-crocodile-bf3zi5/test_def-service-vnwns50w
  Why: test_def-service-vnwns50w looks like a non-production workload and currently uses standard Fargate capacity.
  Action: Switch the capacity provider strategy to FARGATE_SPOT for interrupt-tolerant environments.
- [medium] Review ECS task size for non-production workload
  Resource: arn:aws:ecs:us-east-1:027087672282:service/carefree-crocodile-bf3zi5/test_def-service-vnwns50w
  Why: test_def-service-vnwns50w uses a task definition sized at 1024 CPU units and 3072 MiB memory.
  Action: Validate actual CPU and memory usage and reduce the Fargate task size if headroom is consistently high.
- [medium] Move non-production ECS service to Fargate Spot when interruption is acceptable
  Resource: arn:aws:ecs:us-east-1:027087672282:service/carefree-crocodile-bf3zi5/test_def-service-64zgxsih
  Why: test_def-service-64zgxsih looks like a non-production workload and currently uses standard Fargate capacity.
  Action: Switch the capacity provider strategy to FARGATE_SPOT for interrupt-tolerant environments.

## Unused Or Waste Signals

- [high] Disabled event source mapping may indicate a paused or unused workflow
  Resource: arn:aws:lambda:us-east-1:027087672282:event-source-mapping:55dbb09e-8b4d-4594-8c41-8fa4181ad929
  Why: Mapping from arn:aws:sqs:us-east-1:027087672282:agent-fix-tasks.fifo to arn:aws:lambda:us-east-1:027087672282:function:remediator is disabled.
  Action: Confirm whether this flow is still needed. Remove the mapping or retire the upstream queue/stream if the workflow is gone.
- [high] Enabled event source mapping is failing to process
  Resource: arn:aws:lambda:us-east-1:027087672282:event-source-mapping:3f3bd44c-b2cb-432d-b97c-d34246296a1d
  Why: PROBLEM: Lambda failed to assume your function execution role. Please check if the role trust Lambda service principal, and make sure it does not require setting source identity to be assumed.
  Action: Fix the execution role or disable the mapping until the downstream path is healthy.
- [high] Disabled event source mapping may indicate a paused or unused workflow
  Resource: arn:aws:lambda:us-east-1:027087672282:event-source-mapping:e1eb804b-5d2c-4c56-b1db-4aa5a8107250
  Why: Mapping from arn:aws:sqs:us-east-1:027087672282:connector-tasks.fifo to arn:aws:lambda:us-east-1:027087672282:function:connector-worker is disabled.
  Action: Confirm whether this flow is still needed. Remove the mapping or retire the upstream queue/stream if the workflow is gone.
- [high] Disabled event source mapping may indicate a paused or unused workflow
  Resource: arn:aws:lambda:us-east-1:027087672282:event-source-mapping:715dc62f-47bf-4f1f-b8fb-9d25947dc437
  Why: Mapping from arn:aws:sqs:us-east-1:027087672282:agent-error-queue to arn:aws:lambda:us-east-1:027087672282:function:agent-planner is disabled.
  Action: Confirm whether this flow is still needed. Remove the mapping or retire the upstream queue/stream if the workflow is gone.
- [high] Disabled event source mapping may indicate a paused or unused workflow
  Resource: arn:aws:lambda:us-east-1:027087672282:event-source-mapping:4b361b04-2403-4343-aa61-32e97d094f82
  Why: Mapping from arn:aws:sqs:us-east-1:027087672282:plan-executor-queue to arn:aws:lambda:us-east-1:027087672282:function:plan-executor is disabled.
  Action: Confirm whether this flow is still needed. Remove the mapping or retire the upstream queue/stream if the workflow is gone.

## Anomalies

- No recent billing anomalies were reported.
