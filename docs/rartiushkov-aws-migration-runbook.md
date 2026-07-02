# Rartiushkov AWS Migration Runbook

Эта инструкция про AWS, не про Salesforce. Репозиторий хранит агент и артефакты, которые нужны для миграции AWS-окружения между регионами или аккаунтами: inventory, dependency graph, CloudFormation exports, Lambda ZIP-код, планы развертывания и отчеты проверки.

## Что нужно сначала

1. Обновить локальную AWS-авторизацию. Сейчас у машины виден только профиль `default`, и он должен успешно проходить:

```powershell
aws sts get-caller-identity
```

Если команда падает с `InvalidClientTokenId`, сначала перелогиниться в AWS CLI, SSO или обновить access keys. Скрипты используют обычную цепочку авторизации boto3/AWS CLI, поэтому можно запускать с `AWS_PROFILE`:

```powershell
$env:AWS_PROFILE="default"
aws sts get-caller-identity
```

2. Взять конфиг-шаблон:

```powershell
Copy-Item configs\rartiushkov.aws-migration.example.json configs\rartiushkov.aws-migration.local.json
```

В `configs\rartiushkov.aws-migration.local.json` указать:

- `overrides.source_regions`: регионы, откуда выгружать окружение.
- `overrides.target_region`: регион, куда переносить.
- `source_role_arn`: роль для чтения source AWS, если используется AssumeRole.
- `target_role_arn`: роль для deploy в target AWS, если используется AssumeRole.
- `exclusions`: ресурсы, которые не нужно переносить.

Не коммитить локальный конфиг, если туда попали реальные ARN, ExternalId или служебные детали клиента.

## Выгрузить переносимый пакет

Важно: для полного слепка аккаунта не указывай `--source-env full-account-scan`. Это строковый фильтр по именам ресурсов, а не режим "сканировать все". Для полного экспорта запускай discovery без `--source-env`.

Пример для `us-east-1`:

```powershell
python executor\scripts\discover_aws_environment.py --region us-east-1 --inventory-key full-account-scan-us-east-1 --client-slug rartiushkov --config configs\rartiushkov.aws-migration.local.json
python executor\scripts\scan_environment_risks.py --source-env full-account-scan --region us-east-1 --client-slug rartiushkov
python executor\scripts\export_cloudformation_templates.py --source-env full-account-scan --region us-east-1 --inventory-key full-account-scan-us-east-1 --client-slug rartiushkov --config configs\rartiushkov.aws-migration.local.json
python executor\scripts\export_iac_blueprint.py --source-env full-account-scan --inventory-key full-account-scan-us-east-1 --client-slug rartiushkov
python executor\scripts\export_lambda_code.py --source-env full-account-scan --region us-east-1 --inventory-key full-account-scan-us-east-1 --client-slug rartiushkov --config configs\rartiushkov.aws-migration.local.json
python executor\scripts\export_aws_backup_to_git.py --source-env full-account-scan --inventory-key full-account-scan-us-east-1 --client-slug rartiushkov --config configs\rartiushkov.aws-migration.local.json --output-dir state\clients\rartiushkov\portable_exports\full-account-scan-us-east-1
```

Результат будет лежать в:

```text
state/clients/rartiushkov/aws_inventory/full-account-scan-us-east-1/
state/clients/rartiushkov/portable_exports/full-account-scan-us-east-1/
```

Главные файлы внутри export:

- `snapshots/source_snapshot.json`: полный sanitized inventory AWS.
- `snapshots/summary.json`: счетчики ресурсов.
- `snapshots/dependency_graph.json`: связи Lambda, IAM, SQS, SNS, DynamoDB, API Gateway, ECS, VPC.
- `reports/risk_report.json`: риски перед миграцией.
- `lambda_code/<function>/function.zip`: код Lambda для переносимого восстановления.
- `lambda_code/<function>/configuration.redacted.json`: конфигурация Lambda с редактированными секретными env-переменными.
- `cloudformation_templates/*.json`: исходные шаблоны CloudFormation stack, если stack найден.
- `README.json`: индекс export-пакета.

## Миграция между регионами

Сначала делай read-only plan:

```powershell
python executor\scripts\migrate_account.py --target-env rartiushkov-us-east-2 --source-regions us-east-1 --target-region us-east-2 --client-slug rartiushkov --config configs\rartiushkov.aws-migration.local.json --read-only-plan
```

Если план выглядит нормально, запуск без `--read-only-plan`:

```powershell
python executor\scripts\migrate_account.py --target-env rartiushkov-us-east-2 --source-regions us-east-1 --target-region us-east-2 --client-slug rartiushkov --config configs\rartiushkov.aws-migration.local.json
```

Отчет появится здесь:

```text
state/clients/rartiushkov/migrations/rartiushkov-us-east-2/migration_report.json
```

По умолчанию защита не дает писать в тот же аккаунт и тот же регион. Для sandbox-клонирования в тот же регион нужен осознанный override `allow_same_scope=true`.

## Карта проекта

- `runner/runner.py`: верхний runner агента, который сканирует состояние и вызывает исполнение шагов.
- `cli/agent.py`: CLI-вход для запуска агента по текстовой цели.
- `scanner/aws_scanner.py`: базовое чтение состояния AWS для runner flow.
- `executor/command_runner.py`, `executor/command_guard.py`: выполнение shell/AWS команд и guard rails.
- `executor/action_executor.py`, `executor/actions.py`: прикладные actions агента.
- `executor/scripts/discover_aws_environment.py`: основной discovery AWS. Собирает Lambda, IAM, SQS, SNS, Secrets Manager, DynamoDB, API Gateway, CodeBuild, S3, CloudFormation, ALB, EC2, VPC, RDS, ECS и dependency graph.
- `executor/scripts/export_lambda_code.py`: сохраняет Lambda deployment ZIP и redacted конфигурацию в inventory.
- `executor/scripts/export_cloudformation_templates.py`: выгружает CloudFormation templates найденных stack.
- `executor/scripts/export_iac_blueprint.py`: строит IaC blueprint и starter stubs для Terraform/CloudFormation.
- `executor/scripts/export_aws_backup_to_git.py`: собирает Git-friendly portable export из snapshot, reports и Lambda code.
- `executor/scripts/migrate_account.py`: orchestration для multi-region/cross-region миграции.
- `executor/scripts/deploy_discovered_env.py`: создает target-ресурсы из discovery snapshot.
- `executor/scripts/validate_deployed_env.py`: проверяет результат после deploy.
- `executor/scripts/transfer_s3_objects.py`: переносит S3 objects после создания buckets.
- `executor/scripts/scan_environment_risks.py`: security/migration risk report.
- `executor/scripts/analyze_*`: cost, KMS, performance и unused-resource анализ.
- `executor/scripts/build_*_plan.py`: дополнительные планы миграции: network, strategy, CloudFormation import, advanced migration.
- `executor/scripts/destroy_deployed_env.py`: удаление созданного target deployment по manifest.
- `configs/`: шаблоны конфигов доступа, регионов, exclusions и Git export.
- `state/clients/<client_slug>/`: локальные inventory, deployments, migrations, audit и export-артефакты.
- `docs/`: инструкции по архитектуре, безопасности, ролям и runbooks.
- `tests/`: unit tests для миграционного движка.

## Как агенту использовать это в другом AWS

1. Скопировать репозиторий.
2. Настроить AWS CLI или AssumeRole для нового source/target.
3. Создать local config по шаблону `configs/rartiushkov.aws-migration.example.json`.
4. Запустить discovery и export, чтобы получить переносимый пакет.
5. Запустить `migrate_account.py --read-only-plan`.
6. Проверить план и риски.
7. Запустить migration apply.
8. Проверить `validate_deployed_env.py` и отчеты в `state/clients/<client_slug>/`.

## Важное про секреты

Скрипты не сохраняют значения Secrets Manager. `export_lambda_code.py` редактирует Lambda env-переменные с именами вроде `TOKEN`, `PASSWORD`, `SECRET`, `PRIVATE_KEY`. Но перед передачей export-пакета все равно проверь `source_snapshot.json`, потому что некоторые приложения хранят секреты в нестандартных именах переменных.

## Результат выгрузки на 2026-05-14

AWS аккаунт: `027087672282`  
Регион inventory: `us-east-1`  
Inventory key: `full-account-scan-us-east-1`

Собрано в `summary.json`:

- `lambda_functions`: 12
- `lambda_event_source_mappings`: 7
- `dynamodb_tables`: 9
- `sqs_queues`: 7
- `ecs_clusters`: 5
- `ecs_services`: 5
- `s3_buckets`: 2
- `cloudformation_stacks`: 1

Экспорт кода Lambda:

- `exported_count`: 12
- `failed_count`: 0
- manifest: `state/clients/rartiushkov/aws_inventory/full-account-scan-us-east-1/lambda_code/lambda_code_manifest.json`

Portable export папка:

- `state/clients/rartiushkov/portable_exports/full-account-scan-us-east-1/`
