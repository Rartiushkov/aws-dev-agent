# Why Is It Slow

Generated: 2026-03-21T21:05:04.072569+00:00

## Headline

Most likely slowdown drivers: an event-driven Lambda consumer is broken; an ECS service cannot start healthy tasks; an ECS service cannot start healthy tasks.

## Top Incidents

- [high] ECS service has desired tasks but no healthy running capacity (2 related findings)
  Resource: arn:aws:ecs:us-east-1:027087672282:service/carefree-crocodile-bf3zi5/test_def-service-vnwns50w
  Impact: 2 related findings across 2 resources
  Business impact: Multiple ECS services in the same deployment path are unavailable, so client-facing features or internal jobs on that path may be down.
  Probable cause: test_def-service-vnwns50w cannot start healthy tasks, so the service path is unavailable or degraded.
  Action: Fix image pull, task definition, IAM, networking, or dependency startup issues before scaling traffic back to the service.
  Dependency chain: test_def-service-vnwns50w -> test_def -> arn:aws:iam::027087672282:role/ecsTaskExecutionRole
- [high] Lambda event source is failing to process records
  Resource: arn:aws:lambda:us-east-1:027087672282:event-source-mapping:3f3bd44c-b2cb-432d-b97c-d34246296a1d
  Business impact: Events from arn:aws:dynamodb:us-east-1:027087672282:table/agent_errors/stream/2026-02-23T07:46:15.921 are likely not being processed by task-dispatcher, so async workflows can stall or fall behind.
  Probable cause: task-dispatcher cannot successfully consume from its event source.
  Action: Fix the execution role or permissions, then re-enable healthy processing and drain backlog.
  Dependency chain: arn:aws:dynamodb:us-east-1:027087672282:table/agent_errors/stream/2026-02-23T07:46:15.921 -> 3f3bd44c-b2cb-432d-b97c-d34246296a1d -> task-dispatcher -> arn:aws:iam::027087672282:role/task-dispatcher-role
- [medium] ECS service has no recent utilization datapoints in the snapshot (2 related findings)
  Resource: arn:aws:ecs:us-east-1:027087672282:service/carefree-crocodile-bf3zi5/test_def-service-vnwns50w
  Impact: 2 related findings across 2 resources
  Business impact: The service can still be running, but there is not enough telemetry to tell whether it is under-sized, over-sized, or intermittently saturated.
  Probable cause: Without CPU or memory history, rightsizing and bottleneck analysis are blind.
  Action: Collect CloudWatch CPU and memory utilization history for this service to confirm whether it is starved or overprovisioned.
  Dependency chain: test_def-service-vnwns50w -> test_def -> arn:aws:iam::027087672282:role/ecsTaskExecutionRole
- [medium] A disabled event source mapping may leave async work unprocessed
  Resource: arn:aws:lambda:us-east-1:027087672282:event-source-mapping:55dbb09e-8b4d-4594-8c41-8fa4181ad929
  Business impact: Work from agent-fix-tasks.fifo is currently paused before it reaches remediator, which can delay async jobs or retries.
  Probable cause: Messages or stream records may not be flowing into the consumer path.
  Action: Confirm whether this path is intentionally paused. If not, restore the mapping after validating the consumer.
  Dependency chain: agent-fix-tasks.fifo -> 55dbb09e-8b4d-4594-8c41-8fa4181ad929 -> remediator -> arn:aws:iam::027087672282:role/remediator-role
- [medium] A disabled event source mapping may leave async work unprocessed
  Resource: arn:aws:lambda:us-east-1:027087672282:event-source-mapping:3f3bd44c-b2cb-432d-b97c-d34246296a1d
  Business impact: Work from arn:aws:dynamodb:us-east-1:027087672282:table/agent_errors/stream/2026-02-23T07:46:15.921 is currently paused before it reaches task-dispatcher, which can delay async jobs or retries.
  Probable cause: Messages or stream records may not be flowing into the consumer path.
  Action: Confirm whether this path is intentionally paused. If not, restore the mapping after validating the consumer.
  Dependency chain: arn:aws:dynamodb:us-east-1:027087672282:table/agent_errors/stream/2026-02-23T07:46:15.921 -> 3f3bd44c-b2cb-432d-b97c-d34246296a1d -> task-dispatcher -> arn:aws:iam::027087672282:role/task-dispatcher-role
