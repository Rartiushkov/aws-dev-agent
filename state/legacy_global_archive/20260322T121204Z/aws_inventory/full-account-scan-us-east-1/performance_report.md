# AWS Performance Report

Generated: 2026-03-21T21:05:04.072569+00:00

## Summary

Findings: 15
Most likely slowdown drivers: an event-driven Lambda consumer is broken; an ECS service cannot start healthy tasks; an ECS service cannot start healthy tasks.

## Performance Findings

- [high] Lambda event source is failing to process records
  Resource: arn:aws:lambda:us-east-1:027087672282:event-source-mapping:3f3bd44c-b2cb-432d-b97c-d34246296a1d
  Evidence: PROBLEM: User: arn:aws:sts::027087672282:assumed-role/task-dispatcher-role/awslambda_427_20260319235532640 is not authorized to perform: dynamodb:DescribeStream on resource: arn:aws:dynamodb:us-east-1:027087672282:table/agent_errors/stream/2026-02-23T07:46:15.921 because no identity-based policy allows the dynamodb:DescribeStream action
  Probable cause: task-dispatcher cannot successfully consume from its event source.
  Action: Fix the execution role or permissions, then re-enable healthy processing and drain backlog.
- [high] ECS service has desired tasks but no healthy running capacity
  Resource: arn:aws:ecs:us-east-1:027087672282:service/carefree-crocodile-bf3zi5/test_def-service-vnwns50w
  Evidence: desiredCount=1, runningCount=0, latest failure=(service test_def-service-vnwns50w) was unable to place a task. Reason: CannotPullContainerError: pull image manifest has been retried 1 time(s): failed to resolve ref docker.io/library/repository:latest: pull access denied, repository does not exist or may require authorization: server message: insufficient_scope: authorization failed.
  Probable cause: test_def-service-vnwns50w cannot start healthy tasks, so the service path is unavailable or degraded.
  Action: Fix image pull, task definition, IAM, networking, or dependency startup issues before scaling traffic back to the service.
- [high] ECS service has desired tasks but no healthy running capacity
  Resource: arn:aws:ecs:us-east-1:027087672282:service/carefree-crocodile-bf3zi5/test_def-service-64zgxsih
  Evidence: desiredCount=1, runningCount=0, latest failure=(service test_def-service-64zgxsih) was unable to place a task. Reason: CannotPullContainerError: pull image manifest has been retried 1 time(s): failed to resolve ref docker.io/library/repository:latest: pull access denied, repository does not exist or may require authorization: server message: insufficient_scope: authorization failed.
  Probable cause: test_def-service-64zgxsih cannot start healthy tasks, so the service path is unavailable or degraded.
  Action: Fix image pull, task definition, IAM, networking, or dependency startup issues before scaling traffic back to the service.
- [medium] A disabled event source mapping may leave async work unprocessed
  Resource: arn:aws:lambda:us-east-1:027087672282:event-source-mapping:55dbb09e-8b4d-4594-8c41-8fa4181ad929
  Evidence: State=Disabled for source arn:aws:sqs:us-east-1:027087672282:agent-fix-tasks.fifo
  Probable cause: Messages or stream records may not be flowing into the consumer path.
  Action: Confirm whether this path is intentionally paused. If not, restore the mapping after validating the consumer.
- [medium] A disabled event source mapping may leave async work unprocessed
  Resource: arn:aws:lambda:us-east-1:027087672282:event-source-mapping:3f3bd44c-b2cb-432d-b97c-d34246296a1d
  Evidence: State=Disabled for source arn:aws:dynamodb:us-east-1:027087672282:table/agent_errors/stream/2026-02-23T07:46:15.921
  Probable cause: Messages or stream records may not be flowing into the consumer path.
  Action: Confirm whether this path is intentionally paused. If not, restore the mapping after validating the consumer.
- [medium] A disabled event source mapping may leave async work unprocessed
  Resource: arn:aws:lambda:us-east-1:027087672282:event-source-mapping:e1eb804b-5d2c-4c56-b1db-4aa5a8107250
  Evidence: State=Disabled for source arn:aws:sqs:us-east-1:027087672282:connector-tasks.fifo
  Probable cause: Messages or stream records may not be flowing into the consumer path.
  Action: Confirm whether this path is intentionally paused. If not, restore the mapping after validating the consumer.
- [medium] A disabled event source mapping may leave async work unprocessed
  Resource: arn:aws:lambda:us-east-1:027087672282:event-source-mapping:715dc62f-47bf-4f1f-b8fb-9d25947dc437
  Evidence: State=Disabled for source arn:aws:sqs:us-east-1:027087672282:agent-error-queue
  Probable cause: Messages or stream records may not be flowing into the consumer path.
  Action: Confirm whether this path is intentionally paused. If not, restore the mapping after validating the consumer.
- [medium] A disabled event source mapping may leave async work unprocessed
  Resource: arn:aws:lambda:us-east-1:027087672282:event-source-mapping:4b361b04-2403-4343-aa61-32e97d094f82
  Evidence: State=Disabled for source arn:aws:sqs:us-east-1:027087672282:plan-executor-queue
  Probable cause: Messages or stream records may not be flowing into the consumer path.
  Action: Confirm whether this path is intentionally paused. If not, restore the mapping after validating the consumer.
- [medium] ECS service has no recent utilization datapoints in the snapshot
  Resource: arn:aws:ecs:us-east-1:027087672282:service/carefree-crocodile-bf3zi5/test_def-service-vnwns50w
  Evidence: task cpu=1024, memory=3072, desiredCount=1
  Probable cause: Without CPU or memory history, rightsizing and bottleneck analysis are blind.
  Action: Collect CloudWatch CPU and memory utilization history for this service to confirm whether it is starved or overprovisioned.
- [medium] ECS service has no recent utilization datapoints in the snapshot
  Resource: arn:aws:ecs:us-east-1:027087672282:service/carefree-crocodile-bf3zi5/test_def-service-64zgxsih
  Evidence: task cpu=1024, memory=3072, desiredCount=1
  Probable cause: Without CPU or memory history, rightsizing and bottleneck analysis are blind.
  Action: Collect CloudWatch CPU and memory utilization history for this service to confirm whether it is starved or overprovisioned.
