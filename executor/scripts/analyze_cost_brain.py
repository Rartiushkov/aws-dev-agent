import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from executor.scripts.analyze_cost_opportunities import build_cost_report
from executor.scripts.analyze_unused_resources import build_unused_resource_report
from executor.scripts.audit_log import append_audit_event
from executor.scripts.transfer_common import inventory_dir_name, inventory_dir_path, load_transfer_config, resolve_client_slug, session_for


SERVICE_CATEGORY_HINTS = {
    "ecs": ("elastic container", "fargate", "ec2-container"),
    "lambda": ("lambda",),
    "s3": ("simple storage", "s3"),
    "dynamodb": ("dynamodb",),
    "cloudwatch": ("cloudwatch", "logs"),
    "codebuild": ("codebuild",),
}


def parse_args():
    parser = argparse.ArgumentParser(description="Build a Cost Brain report from AWS billing data and discovered inventory.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--inventory-key", default="")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--config", default="")
    parser.add_argument("--source-role-arn", default="")
    parser.add_argument("--source-external-id", default="")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def _as_float(amount):
    try:
        return float(amount or 0.0)
    except Exception:
        return 0.0


def _sort_amount_desc(items):
    return sorted(items, key=lambda item: item.get("amount", 0.0), reverse=True)


def _service_category(service_name):
    lowered = str(service_name or "").lower()
    for category, hints in SERVICE_CATEGORY_HINTS.items():
        if any(hint in lowered for hint in hints):
            return category
    return ""


def _get_time_period(days):
    end_date = date.today()
    start_date = end_date - timedelta(days=max(days, 1))
    return {
        "Start": start_date.isoformat(),
        "End": end_date.isoformat(),
    }


def _group_results_to_amounts(results, key_name):
    items = []
    for bucket in results.get("ResultsByTime", []):
        for group in bucket.get("Groups", []):
            keys = group.get("Keys", [])
            amount = _as_float(group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount"))
            if not keys:
                continue
            items.append({
                key_name: keys[0],
                "amount": amount,
                "unit": group.get("Metrics", {}).get("UnblendedCost", {}).get("Unit", "USD"),
            })
    combined = {}
    for item in items:
        key = item[key_name]
        combined.setdefault(key, {"amount": 0.0, "unit": item["unit"]})
        combined[key]["amount"] += item["amount"]
    return [
        {key_name: key, "amount": round(value["amount"], 2), "unit": value["unit"]}
        for key, value in combined.items()
    ]


def collect_cost_data(ce_client, days=30):
    time_period = _get_time_period(days)
    metrics = ["UnblendedCost"]
    total_response = ce_client.get_cost_and_usage(
        TimePeriod=time_period,
        Granularity="MONTHLY",
        Metrics=metrics,
    )
    service_response = ce_client.get_cost_and_usage(
        TimePeriod=time_period,
        Granularity="MONTHLY",
        Metrics=metrics,
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    region_response = ce_client.get_cost_and_usage(
        TimePeriod=time_period,
        Granularity="MONTHLY",
        Metrics=metrics,
        GroupBy=[{"Type": "DIMENSION", "Key": "REGION"}],
    )
    daily_response = ce_client.get_cost_and_usage(
        TimePeriod=time_period,
        Granularity="DAILY",
        Metrics=metrics,
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    forecast = None
    forecast_error = ""
    try:
        forecast = ce_client.get_cost_forecast(
            TimePeriod=time_period,
            Metric="UNBLENDED_COST",
            Granularity="MONTHLY",
        )
    except Exception as exc:
        forecast_error = str(exc)

    anomalies = []
    anomalies_error = ""
    try:
        anomaly_start = (date.today() - timedelta(days=min(days, 14))).isoformat()
        anomaly_end = date.today().isoformat()
        paginator = ce_client.get_paginator("get_anomalies")
        for page in paginator.paginate(DateInterval={"StartDate": anomaly_start, "EndDate": anomaly_end}):
            anomalies.extend(page.get("Anomalies", []))
    except Exception as exc:
        anomalies_error = str(exc)

    total_amount = 0.0
    total_unit = "USD"
    for bucket in total_response.get("ResultsByTime", []):
        total_amount += _as_float(bucket.get("Total", {}).get("UnblendedCost", {}).get("Amount"))
        total_unit = bucket.get("Total", {}).get("UnblendedCost", {}).get("Unit", total_unit)

    by_service = _sort_amount_desc(_group_results_to_amounts(service_response, "service"))
    by_region = _sort_amount_desc(_group_results_to_amounts(region_response, "region"))

    daily = []
    for bucket in daily_response.get("ResultsByTime", []):
        day_services = []
        for group in bucket.get("Groups", []):
            keys = group.get("Keys", [])
            if not keys:
                continue
            day_services.append({
                "service": keys[0],
                "amount": round(_as_float(group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount")), 2),
                "unit": group.get("Metrics", {}).get("UnblendedCost", {}).get("Unit", "USD"),
            })
        daily.append({
            "date": bucket.get("TimePeriod", {}).get("Start"),
            "services": _sort_amount_desc(day_services),
        })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "time_period": time_period,
        "total_cost": {"amount": round(total_amount, 2), "unit": total_unit},
        "service_costs": by_service,
        "region_costs": by_region,
        "daily_costs": daily,
        "forecast": {
            "amount": round(_as_float((forecast or {}).get("Total", {}).get("Amount")), 2),
            "unit": (forecast or {}).get("Total", {}).get("Unit", "USD"),
            "prediction_interval_lower": round(_as_float((forecast or {}).get("ForecastResultsByTime", [{}])[0].get("PredictionIntervalLowerBound", {}).get("Amount")), 2) if forecast else 0.0,
            "prediction_interval_upper": round(_as_float((forecast or {}).get("ForecastResultsByTime", [{}])[0].get("PredictionIntervalUpperBound", {}).get("Amount")), 2) if forecast else 0.0,
            "error": forecast_error,
        },
        "anomalies": [
            {
                "start_date": item.get("AnomalyStartDate"),
                "end_date": item.get("AnomalyEndDate"),
                "impact": round(_as_float(item.get("Impact", {}).get("TotalImpact")), 2),
                "service": (item.get("RootCauses") or [{}])[0].get("Service"),
                "region": (item.get("RootCauses") or [{}])[0].get("Region"),
                "linked_account": (item.get("RootCauses") or [{}])[0].get("LinkedAccount"),
            }
            for item in anomalies
        ],
        "anomalies_error": anomalies_error,
    }


def collect_current_month_cost_data(ce_client):
    today = date.today()
    month_start = today.replace(day=1)
    next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
    time_period = {
        "Start": month_start.isoformat(),
        "End": next_month.isoformat(),
    }
    mtd_response = ce_client.get_cost_and_usage(
        TimePeriod={"Start": month_start.isoformat(), "End": today.isoformat()},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
    )
    service_response = ce_client.get_cost_and_usage(
        TimePeriod=time_period,
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    region_response = ce_client.get_cost_and_usage(
        TimePeriod=time_period,
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "REGION"}],
    )
    daily_response = ce_client.get_cost_and_usage(
        TimePeriod={"Start": month_start.isoformat(), "End": today.isoformat()},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    forecast_amount = 0.0
    forecast_unit = "USD"
    forecast_error = ""
    forecast_daily = []
    try:
        forecast = ce_client.get_cost_forecast(
            TimePeriod={"Start": today.isoformat(), "End": next_month.isoformat()},
            Metric="UNBLENDED_COST",
            Granularity="DAILY",
        )
        forecast_amount = round(_as_float(forecast.get("Total", {}).get("Amount")), 2)
        forecast_unit = forecast.get("Total", {}).get("Unit", "USD")
        forecast_daily = [
            {
                "date": item.get("TimePeriod", {}).get("Start"),
                "amount": round(_as_float(item.get("MeanValue")), 4),
            }
            for item in forecast.get("ForecastResultsByTime", [])
        ]
    except Exception as exc:
        forecast_error = str(exc)

    mtd_total = 0.0
    total_unit = "USD"
    for bucket in mtd_response.get("ResultsByTime", []):
        mtd_total += _as_float(bucket.get("Total", {}).get("UnblendedCost", {}).get("Amount"))
        total_unit = bucket.get("Total", {}).get("UnblendedCost", {}).get("Unit", total_unit)

    return {
        "month_start": month_start.isoformat(),
        "today": today.isoformat(),
        "month_end": next_month.isoformat(),
        "month_to_date_cost": {"amount": round(mtd_total, 4), "unit": total_unit},
        "full_month_service_costs": _sort_amount_desc(_group_results_to_amounts(service_response, "service")),
        "full_month_region_costs": _sort_amount_desc(_group_results_to_amounts(region_response, "region")),
        "month_to_date_daily_costs": [
            {
                "date": bucket.get("TimePeriod", {}).get("Start"),
                "services": _sort_amount_desc([
                    {
                        "service": group.get("Keys", [""])[0],
                        "amount": round(_as_float(group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount")), 6),
                        "unit": group.get("Metrics", {}).get("UnblendedCost", {}).get("Unit", "USD"),
                    }
                    for group in bucket.get("Groups", [])
                    if group.get("Keys")
                ]),
            }
            for bucket in daily_response.get("ResultsByTime", [])
        ],
        "month_end_forecast": {
            "amount": forecast_amount,
            "unit": forecast_unit,
            "daily": forecast_daily,
            "error": forecast_error,
        },
    }


def _estimate_savings(opportunity, service_cost_map, service_counts):
    category = opportunity.get("category", "")
    if category == "ecs-waste":
        ecs_cost = service_cost_map.get("ecs", 0.0)
        divisor = max(service_counts.get("ecs_services", 1), 1)
        return round(ecs_cost / divisor, 2)
    if category == "ecs-spot":
        ecs_cost = service_cost_map.get("ecs", 0.0)
        divisor = max(service_counts.get("ecs_services", 1), 1)
        return round((ecs_cost / divisor) * 0.5, 2)
    if category == "ecs-rightsizing":
        ecs_cost = service_cost_map.get("ecs", 0.0)
        divisor = max(service_counts.get("ecs_services", 1), 1)
        return round((ecs_cost / divisor) * 0.3, 2)
    if category == "resource-cleanup":
        lambda_cost = service_cost_map.get("lambda", 0.0)
        divisor = max(service_counts.get("lambda_functions", 1), 1)
        return round(lambda_cost / divisor, 2)
    if category == "s3-lifecycle":
        return round(service_cost_map.get("s3", 0.0) * 0.15, 2)
    if category == "lambda-graviton":
        lambda_cost = service_cost_map.get("lambda", 0.0)
        divisor = max(service_counts.get("lambda_functions", 1), 1)
        return round((lambda_cost / divisor) * 0.2, 2)
    return 0.0


def build_cost_brain_report(snapshot, cost_data, current_month=None):
    opportunity_report = build_cost_report(snapshot)
    unused_report = build_unused_resource_report(snapshot)
    counts = {
        "ecs_services": len(snapshot.get("ecs", {}).get("services", [])),
        "lambda_functions": len(snapshot.get("lambda_functions", [])),
    }
    service_cost_map = {}
    for item in cost_data.get("service_costs", []):
        category = _service_category(item.get("service"))
        if not category:
            continue
        service_cost_map[category] = service_cost_map.get(category, 0.0) + item.get("amount", 0.0)

    enriched = []
    for item in opportunity_report.get("opportunities", []):
        estimated = _estimate_savings(item, service_cost_map, counts)
        enriched.append({
            **item,
            "estimated_monthly_savings": estimated,
            "estimated_unit": cost_data.get("total_cost", {}).get("unit", "USD"),
        })
    enriched = sorted(
        enriched,
        key=lambda item: (item.get("estimated_monthly_savings", 0.0), {"high": 3, "medium": 2, "low": 1}.get(item.get("impact"), 0)),
        reverse=True,
    )

    top_services = cost_data.get("service_costs", [])[:5]
    top_regions = cost_data.get("region_costs", [])[:5]
    top_anomalies = sorted(cost_data.get("anomalies", []), key=lambda item: item.get("impact", 0.0), reverse=True)[:5]

    total_estimated_savings = round(sum(item.get("estimated_monthly_savings", 0.0) for item in enriched), 2)

    summary_lines = []
    if top_services:
        main = top_services[0]
        summary_lines.append(
            f"Top service cost in the last {cost_data.get('window_days', 30)} days was {main.get('service')} at {main.get('amount')} {main.get('unit')}."
        )
    if enriched:
        top = enriched[0]
        summary_lines.append(
            f"Top optimization candidate is '{top.get('title')}' with estimated savings of about {top.get('estimated_monthly_savings')} {top.get('estimated_unit')} per month."
        )
    if top_anomalies:
        summary_lines.append(
            f"Highest recent anomaly impact was about {top_anomalies[0].get('impact')} {cost_data.get('total_cost', {}).get('unit', 'USD')}."
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_env": snapshot.get("source_env", ""),
        "region": snapshot.get("region", ""),
        "account_id": snapshot.get("account_id", ""),
        "billing_window_days": cost_data.get("window_days", 30),
        "summary": {
            "total_cost_last_window": cost_data.get("total_cost", {}),
            "forecast_next_window": cost_data.get("forecast", {}),
            "current_month_to_date_cost": (current_month or {}).get("month_to_date_cost", {}),
            "current_month_forecast": (current_month or {}).get("month_end_forecast", {}),
            "estimated_monthly_savings": {
                "amount": total_estimated_savings,
                "unit": cost_data.get("total_cost", {}).get("unit", "USD"),
            },
            "top_service_count": len(top_services),
            "recommendation_count": len(enriched),
            "automation_ready_count": opportunity_report.get("summary", {}).get("automation_ready_count", 0),
        },
        "summary_lines": summary_lines,
        "top_service_costs": top_services,
        "top_region_costs": top_regions,
        "current_month_top_service_costs": ((current_month or {}).get("full_month_service_costs") or [])[:5],
        "current_month_top_region_costs": ((current_month or {}).get("full_month_region_costs") or [])[:5],
        "recent_anomalies": top_anomalies,
        "recommendations": enriched[:10],
        "unused_or_waste_summary": unused_report.get("summary", {}),
        "unused_or_waste_findings": unused_report.get("findings", []),
        "strengths": opportunity_report.get("strengths", []),
        "next_level_requirements": opportunity_report.get("next_level_requirements", []),
        "billing_data_health": {
            "forecast_error": cost_data.get("forecast", {}).get("error", ""),
            "anomalies_error": cost_data.get("anomalies_error", ""),
        },
    }


def build_client_cost_report(brain_report):
    summary = brain_report.get("summary", {})
    month_to_date = summary.get("current_month_to_date_cost", {})
    month_forecast = summary.get("current_month_forecast", {})
    service_breakdown = brain_report.get("current_month_top_service_costs", []) or brain_report.get("top_service_costs", [])
    region_breakdown = brain_report.get("current_month_top_region_costs", []) or brain_report.get("top_region_costs", [])
    recommendations = brain_report.get("recommendations", [])[:5]

    return {
        "generated_at": brain_report.get("generated_at"),
        "account_id": brain_report.get("account_id", ""),
        "region": brain_report.get("region", ""),
        "period": {
            "month_to_date_amount": month_to_date.get("amount", 0.0),
            "month_to_date_unit": month_to_date.get("unit", "USD"),
            "month_end_forecast_amount": month_forecast.get("amount", 0.0),
            "month_end_forecast_unit": month_forecast.get("unit", "USD"),
        },
        "service_breakdown": service_breakdown,
        "region_breakdown": region_breakdown,
        "recent_anomalies": brain_report.get("recent_anomalies", []),
        "recommended_actions": [
            {
                "priority": item.get("impact"),
                "title": item.get("title"),
                "resource_id": item.get("resource_id"),
                "why": item.get("rationale"),
                "action": item.get("recommendation"),
                "automation_ready": item.get("automation_ready", False),
            }
            for item in recommendations
        ],
        "unused_or_waste_findings": brain_report.get("unused_or_waste_findings", [])[:5],
        "client_summary": {
            "headline": (
                f"Month-to-date AWS spend is {month_to_date.get('amount', 0.0)} {month_to_date.get('unit', 'USD')}, "
                f"with a forecast of {month_forecast.get('amount', 0.0)} {month_forecast.get('unit', 'USD')} by month end."
            ),
            "top_cost_service": service_breakdown[0]["service"] if service_breakdown else "",
            "top_cost_region": region_breakdown[0]["region"] if region_breakdown else "",
            "recommendation_count": len(recommendations),
            "unused_or_waste_count": len(brain_report.get("unused_or_waste_findings", [])),
        },
    }


def build_client_cost_markdown(client_report):
    lines = [
        "# AWS Cost Report",
        "",
        f"Generated: {client_report.get('generated_at', '')}",
        "",
        "## Summary",
        "",
        client_report.get("client_summary", {}).get("headline", ""),
        "",
        "## Service Breakdown",
        "",
    ]
    for item in client_report.get("service_breakdown", []):
        lines.append(f"- {item.get('service')}: {item.get('amount')} {item.get('unit')}")
    lines.extend([
        "",
        "## Region Breakdown",
        "",
    ])
    for item in client_report.get("region_breakdown", []):
        lines.append(f"- {item.get('region')}: {item.get('amount')} {item.get('unit')}")
    lines.extend([
        "",
        "## Recommended Actions",
        "",
    ])
    for item in client_report.get("recommended_actions", []):
        lines.append(f"- [{item.get('priority')}] {item.get('title')}")
        lines.append(f"  Resource: {item.get('resource_id')}")
        lines.append(f"  Why: {item.get('why')}")
        lines.append(f"  Action: {item.get('action')}")
    lines.extend([
        "",
        "## Unused Or Waste Signals",
        "",
    ])
    waste_findings = client_report.get("unused_or_waste_findings", [])
    if not waste_findings:
        lines.append("- No likely unused resources were detected in the current snapshot.")
    else:
        for item in waste_findings:
            lines.append(f"- [{item.get('confidence')}] {item.get('title')}")
            lines.append(f"  Resource: {item.get('resource_id')}")
            lines.append(f"  Why: {item.get('why')}")
            lines.append(f"  Action: {item.get('recommended_action')}")
    anomalies = client_report.get("recent_anomalies", [])
    lines.extend([
        "",
        "## Anomalies",
        "",
    ])
    if not anomalies:
        lines.append("- No recent billing anomalies were reported.")
    else:
        for item in anomalies:
            lines.append(f"- {item.get('service') or 'unknown service'} in {item.get('region') or 'unknown region'}: {item.get('impact')} USD")
    return "\n".join(lines) + "\n"


def main():
    args = parse_args()
    config = load_transfer_config(args.config)
    client_slug = resolve_client_slug(args.client_slug, config, source_env=args.source_env)
    inventory_name = inventory_dir_name(args.source_env, args.inventory_key)
    inventory_dir = inventory_dir_path(args.source_env, args.inventory_key, client_slug)
    snapshot = json.loads((inventory_dir / "source_snapshot.json").read_text(encoding="utf-8"))
    source_external_id = args.source_external_id or config.get("overrides", {}).get("source_external_id", "")
    billing_session = session_for(args.region, args.source_role_arn, external_id=source_external_id)
    ce_client = billing_session.client("ce", region_name="us-east-1")

    try:
        cost_data = collect_cost_data(ce_client, days=args.days)
    except NoCredentialsError:
        print(json.dumps({
            "status": "failed",
            "error": "missing_credentials",
            "message": "AWS credentials were not found. Configure credentials or assume a source role before running Cost Brain analysis.",
            "required_permissions": [
                "ce:GetCostAndUsage",
                "ce:GetCostForecast",
                "ce:GetAnomalies",
                "cloudwatch:GetMetricData",
            ],
        }, indent=2))
        raise SystemExit(1)
    except ClientError as exc:
        print(json.dumps({
            "status": "failed",
            "error": "aws_client_error",
            "message": str(exc),
            "required_permissions": [
                "ce:GetCostAndUsage",
                "ce:GetCostForecast",
                "ce:GetAnomalies",
                "cloudwatch:GetMetricData",
            ],
        }, indent=2))
        raise SystemExit(1)

    current_month = collect_current_month_cost_data(ce_client)
    cost_report = build_cost_brain_report(snapshot, cost_data, current_month=current_month)

    client_report = build_client_cost_report(cost_report)
    unused_report = build_unused_resource_report(snapshot)

    cost_data_path = inventory_dir / "cost_breakdown_report.json"
    brain_report_path = inventory_dir / "cost_brain_report.json"
    unused_report_path = inventory_dir / "unused_resource_report.json"
    client_report_path = inventory_dir / "client_cost_report.json"
    client_markdown_path = inventory_dir / "client_cost_report.md"
    cost_data_path.write_text(json.dumps(cost_data, indent=2), encoding="utf-8")
    brain_report_path.write_text(json.dumps(cost_report, indent=2), encoding="utf-8")
    unused_report_path.write_text(json.dumps(unused_report, indent=2), encoding="utf-8")
    client_report_path.write_text(json.dumps(client_report, indent=2), encoding="utf-8")
    client_markdown_path.write_text(build_client_cost_markdown(client_report), encoding="utf-8")

    append_audit_event(
        "analyze_cost_brain",
        "ok",
        {
            "cost_data_path": str(cost_data_path),
            "brain_report_path": str(brain_report_path),
            "unused_report_path": str(unused_report_path),
            "client_report_path": str(client_report_path),
            "client_markdown_path": str(client_markdown_path),
            "days": args.days,
            "month_to_date_cost": current_month.get("month_to_date_cost", {}),
        },
        source_env=inventory_name,
        client_slug=client_slug,
    )
    print(json.dumps({
        "status": "ok",
        "cost_data_path": str(cost_data_path),
        "brain_report_path": str(brain_report_path),
        "unused_report_path": str(unused_report_path),
        "client_report_path": str(client_report_path),
        "client_markdown_path": str(client_markdown_path),
        "total_cost": cost_data.get("total_cost", {}),
        "recommendation_count": len(cost_report.get("recommendations", [])),
    }, indent=2))


if __name__ == "__main__":
    main()
