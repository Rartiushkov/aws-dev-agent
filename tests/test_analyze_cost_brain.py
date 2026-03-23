import unittest

from executor.scripts.analyze_cost_brain import build_client_cost_markdown, build_client_cost_report, build_cost_brain_report, collect_cost_data


class FakeCePaginator:
    def paginate(self, **kwargs):
        return [{
            "Anomalies": [
                {
                    "AnomalyStartDate": "2026-03-10",
                    "AnomalyEndDate": "2026-03-11",
                    "Impact": {"TotalImpact": "42.5"},
                    "RootCauses": [{"Service": "AWS Lambda", "Region": "us-east-1", "LinkedAccount": "123456789012"}],
                }
            ]
        }]


class FakeCeClient:
    def get_cost_and_usage(self, **kwargs):
        group_by = kwargs.get("GroupBy", [])
        granularity = kwargs.get("Granularity")
        if not group_by:
            return {
                "ResultsByTime": [
                    {"Total": {"UnblendedCost": {"Amount": "250.00", "Unit": "USD"}}}
                ]
            }
        key = group_by[0]["Key"]
        if key == "SERVICE" and granularity == "MONTHLY":
            return {
                "ResultsByTime": [
                    {
                        "Groups": [
                            {"Keys": ["AWS Lambda"], "Metrics": {"UnblendedCost": {"Amount": "50.00", "Unit": "USD"}}},
                            {"Keys": ["Amazon Simple Storage Service"], "Metrics": {"UnblendedCost": {"Amount": "40.00", "Unit": "USD"}}},
                            {"Keys": ["Amazon Elastic Container Service"], "Metrics": {"UnblendedCost": {"Amount": "120.00", "Unit": "USD"}}},
                        ]
                    }
                ]
            }
        if key == "REGION":
            return {
                "ResultsByTime": [
                    {
                        "Groups": [
                            {"Keys": ["us-east-1"], "Metrics": {"UnblendedCost": {"Amount": "200.00", "Unit": "USD"}}},
                            {"Keys": ["us-west-2"], "Metrics": {"UnblendedCost": {"Amount": "50.00", "Unit": "USD"}}},
                        ]
                    }
                ]
            }
        if key == "SERVICE" and granularity == "DAILY":
            return {
                "ResultsByTime": [
                    {
                        "TimePeriod": {"Start": "2026-03-20"},
                        "Groups": [
                            {"Keys": ["AWS Lambda"], "Metrics": {"UnblendedCost": {"Amount": "2.00", "Unit": "USD"}}}
                        ],
                    }
                ]
            }
        return {"ResultsByTime": []}

    def get_cost_forecast(self, **kwargs):
        return {
            "Total": {"Amount": "270.00", "Unit": "USD"},
            "ForecastResultsByTime": [
                {
                    "PredictionIntervalLowerBound": {"Amount": "240.00"},
                    "PredictionIntervalUpperBound": {"Amount": "300.00"},
                }
            ],
        }

    def get_paginator(self, name):
        self.paginator_name = name
        return FakeCePaginator()


class AnalyzeCostBrainTests(unittest.TestCase):

    def test_collect_cost_data_parses_service_region_and_anomalies(self):
        report = collect_cost_data(FakeCeClient(), days=30)

        self.assertEqual(report["total_cost"]["amount"], 250.0)
        self.assertEqual(report["service_costs"][0]["service"], "Amazon Elastic Container Service")
        self.assertEqual(report["region_costs"][0]["region"], "us-east-1")
        self.assertEqual(report["forecast"]["amount"], 270.0)
        self.assertEqual(report["anomalies"][0]["impact"], 42.5)

    def test_build_cost_brain_report_combines_billing_with_recommendations(self):
        snapshot = {
            "source_env": "legacy",
            "region": "us-east-1",
            "account_id": "123456789012",
            "lambda_functions": [
                {"FunctionName": "hello-world-fn", "FunctionArn": "arn:lambda:hello", "Runtime": "python3.12", "Architectures": ["x86_64"]}
            ],
            "s3_buckets": [{"Name": "artifacts", "LifecycleRules": []}],
            "dynamodb_tables": [{"Table": {"BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"}}}],
            "ecs": {
                "services": [
                    {
                        "serviceName": "test-api",
                        "serviceArn": "arn:ecs:test-api",
                        "desiredCount": 1,
                        "runningCount": 0,
                        "capacityProviderStrategy": [{"capacityProvider": "FARGATE", "weight": 1}],
                        "taskDefinition": "td-1",
                        "events": [{"message": "unable to place a task"}, {"message": "failed to start"}],
                    }
                ],
                "task_definitions": [{"taskDefinitionArn": "td-1", "cpu": "1024", "memory": "2048"}],
            },
        }
        cost_data = collect_cost_data(FakeCeClient(), days=30)

        report = build_cost_brain_report(snapshot, cost_data)

        self.assertEqual(report["summary"]["total_cost_last_window"]["amount"], 250.0)
        self.assertTrue(report["recommendations"])
        self.assertGreater(report["recommendations"][0]["estimated_monthly_savings"], 0.0)
        self.assertTrue(report["summary_lines"])

    def test_build_client_cost_report_exposes_service_breakdown_for_clients(self):
        snapshot = {
            "source_env": "legacy",
            "region": "us-east-1",
            "account_id": "123456789012",
            "lambda_functions": [],
            "s3_buckets": [],
            "dynamodb_tables": [],
            "ecs": {"services": [], "task_definitions": []},
        }
        cost_data = collect_cost_data(FakeCeClient(), days=30)
        brain_report = build_cost_brain_report(snapshot, cost_data, current_month={
            "month_to_date_cost": {"amount": 12.34, "unit": "USD"},
            "month_end_forecast": {"amount": 18.9, "unit": "USD", "daily": [], "error": ""},
            "full_month_service_costs": [{"service": "Amazon Elastic Container Service", "amount": 8.0, "unit": "USD"}],
            "full_month_region_costs": [{"region": "us-east-1", "amount": 12.0, "unit": "USD"}],
        })

        client_report = build_client_cost_report(brain_report)
        markdown = build_client_cost_markdown(client_report)

        self.assertEqual(client_report["period"]["month_to_date_amount"], 12.34)
        self.assertEqual(client_report["service_breakdown"][0]["service"], "Amazon Elastic Container Service")
        self.assertIn("Service Breakdown", markdown)
        self.assertIn("Amazon Elastic Container Service", markdown)
        self.assertIn("Unused Or Waste Signals", markdown)


if __name__ == "__main__":
    unittest.main()
