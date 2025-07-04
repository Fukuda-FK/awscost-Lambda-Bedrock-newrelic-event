import boto3
import json
import os
import gzip
import logging
import time
import random
import http.client
from datetime import datetime, date, timedelta
import calendar
from typing import Dict, List, Any

# =================================================================
# ロガーとBoto3クライアントの初期化 (グローバル)
# =================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

# 環境変数の読み込み
NEW_RELIC_LICENSE_KEY = os.environ.get('NEW_RELIC_LICENSE_KEY')
NEW_RELIC_ACCOUNT_ID = os.environ.get('NEW_RELIC_ACCOUNT_ID')
TARGET_REGION = os.environ.get('TARGET_REGION', 'us-east-1')
GROUP_BY_DIMENSION_KEY = os.environ.get('GROUP_BY_DIMENSION_KEY', 'SERVICE')
GROUP_BY_TAG_KEY = os.environ.get('GROUP_BY_TAG_KEY')
BEDROCK_MODEL_ID = os.environ.get('BEDROCK_MODEL_ID', 'anthropic.claude-3-haiku-20240307-v1:0')
BEDROCK_REGION = os.environ.get('BEDROCK_REGION', 'us-east-1')
JPY_EXCHANGE_RATE = int(os.environ.get('JPY_EXCHANGE_RATE', '150'))

# クライアントは一度だけ初期化して再利用する
ce_client = boto3.client('ce', region_name=TARGET_REGION)
coh_client = boto3.client('cost-optimization-hub', region_name=TARGET_REGION)
bedrock_client = boto3.client('bedrock-runtime', region_name=BEDROCK_REGION)

# =================================================================
# ヘルパー関数
# =================================================================
def to_camel_case(snake_str: str) -> str:
    if not isinstance(snake_str, str) or not snake_str: return ""
    components = snake_str.lower().split('_')
    return components[0] + ''.join(x.title() for x in components[1:])

def json_serial_datetime(obj):
    if isinstance(obj, (datetime, date)): return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")

# =================================================================
# ワークフロー1: コスト実績の処理
# =================================================================
def run_cost_explorer_workflow(aws_account_id: str) -> List[Dict]:
    logger.info("--- Running Cost Explorer Workflow ---")
    events = []

    group_by_definitions = []
    if GROUP_BY_DIMENSION_KEY:
        group_by_definitions.append({'Type': 'DIMENSION', 'Key': GROUP_BY_DIMENSION_KEY})
    group_by_definitions.append({'Type': 'DIMENSION', 'Key': 'REGION'})
    if GROUP_BY_TAG_KEY:
        group_by_definitions.append({'Type': 'TAG', 'Key': GROUP_BY_TAG_KEY})

    # ★★★ 月次集計のロジックに変更 ★★★
    today = datetime.utcnow().date()
    is_first_of_month = (today.day == 1)

    if is_first_of_month:
        # 1日の場合、前月全体のデータを取得
        end_date_for_api = today.replace(day=1)
        last_month_end = end_date_for_api - timedelta(days=1)
        start_date = last_month_end.replace(day=1)
    else:
        # 2日以降の場合、当月の月初から前日までのデータを取得
        start_date = today.replace(day=1)
        end_date_for_api = today

    logger.info(f"Fetching monthly cost data from {start_date.isoformat()} to {end_date_for_api.isoformat()}")
    
    all_groups = []
    next_page_token = None
    while True:
        params = {
            'TimePeriod': {'Start': start_date.isoformat(), 'End': end_date_for_api.isoformat()},
            'Granularity': 'MONTHLY', # GranularityをMONTHLYに戻す
            'Metrics': ['UnblendedCost'],
            'GroupBy': group_by_definitions
        }
        if next_page_token: params['NextPageToken'] = next_page_token
        response = ce_client.get_cost_and_usage(**params)
        if response.get('ResultsByTime'):
            all_groups.extend(response['ResultsByTime'][0]['Groups'])
        next_page_token = response.get('NextPageToken')
        if not next_page_token: break

    cost_data = {'ResultsByTime': [{'TimePeriod': {'Start': start_date.isoformat(), 'End': end_date_for_api.isoformat()}, 'Groups': all_groups}] if all_groups else []}

    # ★★★ コスト予測を当月の場合のみに限定 ★★★
    forecast_data = []
    if not is_first_of_month:
        try:
            last_day_of_month = calendar.monthrange(today.year, today.month)[1]
            forecast_end_date = today.replace(day=last_day_of_month)
            logger.info(f"Fetching monthly cost forecast for current month.")
            forecast_response = ce_client.get_cost_forecast(
                TimePeriod={'Start': today.isoformat(), 'End': (forecast_end_date + timedelta(days=1)).isoformat()},
                Metric='UNBLENDED_COST',
                Granularity='MONTHLY'
            )
            forecast_data = forecast_response.get('ForecastResultsByTime', [])
        except ce_client.exceptions.DataUnavailableException as e:
            logger.warning(f"Could not retrieve cost forecast: {e}")

    if not cost_data.get('ResultsByTime') and not forecast_data:
        logger.warning("No actual cost data or forecast data found.")
        return events

    month_result = cost_data.get('ResultsByTime', [{}])[0]
    time_period = month_result.get('TimePeriod', {'Start': start_date.isoformat(), 'End': end_date_for_api.isoformat()})
    total_cost = sum(float(g['Metrics']['UnblendedCost']['Amount']) for g in month_result.get('Groups', []))

    for group in month_result.get('Groups', []):
        cost_metrics = group.get('Metrics', {}).get('UnblendedCost', {})
        event_detail = {
            'eventType': 'AwsCostReport', 'recordType': 'detail', 'aws.accountId': aws_account_id,
            'aws.lambdaRegion': TARGET_REGION, 'period.start': time_period.get('Start'),
            'period.end': time_period.get('End'), 'cost.unblended': float(cost_metrics.get('Amount', 0)),
            'cost.currency': cost_metrics.get('Unit', 'USD')
        }
        keys = group.get('Keys', [])
        key_index = 0
        if GROUP_BY_DIMENSION_KEY and len(keys) > key_index:
            event_detail[f"aws.{to_camel_case(GROUP_BY_DIMENSION_KEY)}"] = keys[key_index]
            key_index += 1
        if len(keys) > key_index:
            event_detail['aws.region'] = keys[key_index]
            key_index += 1
        if GROUP_BY_TAG_KEY and len(keys) > key_index:
            event_detail[f"aws.tag.{GROUP_BY_TAG_KEY.replace('$', '_').replace(':', '_')}"] = keys[key_index]
        events.append(event_detail)

    forecast_amount = float(forecast_data[0].get('MeanValue', 0.0)) if forecast_data else 0.0
    summary_event = {
        'eventType': 'AwsCostReport', 'recordType': 'summary', 'aws.accountId': aws_account_id,
        'aws.lambdaRegion': TARGET_REGION, 'period.start': time_period.get('Start'),
        'period.end': time_period.get('End'), 'cost.totalUnblended': total_cost,
        'cost.monthlyForecast': forecast_amount
    }

    top_5_groups = sorted(all_groups, key=lambda x: float(x.get('Metrics', {}).get('UnblendedCost', {}).get('Amount', 0)), reverse=True)[:5]
    top_cost_drivers = []
    for group in top_5_groups:
        cost_usd = float(group.get('Metrics', {}).get('UnblendedCost', {}).get('Amount', 0))
        driver = {"group": group.get('Keys', []), "cost_usd": cost_usd, "cost_jpy": round(cost_usd * JPY_EXCHANGE_RATE)}
        top_cost_drivers.append(driver)
    
    # ★★★ Bedrockへの入力とプロンプトを月次報告用に修正 ★★★
    bedrock_input_data = {
        "aws_account_id": aws_account_id, "period": time_period,
        "cost": {
            "total_unblended_usd": total_cost,
            "monthly_forecast_usd": forecast_amount,
            "total_unblended_jpy": round(total_cost * JPY_EXCHANGE_RATE),
            "monthly_forecast_jpy": round(forecast_amount * JPY_EXCHANGE_RATE)
        },
        "top_cost_drivers": top_cost_drivers,
        "monthly_budget": {"amount": 10000, "currency": "JPY"}
    }
    
    system_prompt = """あなたは優秀なFinOps専門家です。提供されたAWSの月次コストデータを分析し、IT管理者向けに報告書をJSON形式で作成してください。
あなたのタスクは以下の通りです:
1. 提供された`monthly_budget`（円）と、`cost`に含まれる実績コストおよび予測コスト（`monthly_forecast_jpy`が0より大きい場合）を比較し、予算超過のリスクを評価します。
2. `top_cost_drivers`を分析し、コストの主要因となっているサービスとリージョンを特定します。
3. 上記の分析結果に基づき、「状況の要約」「潜在的リスク」「推奨アクション」を簡潔に日本語で記述します。
4. 金額について言及する際は、ドル(USD)と日本円(JPY)の値を併記してください。"""

    human_prompt_data_str = json.dumps(bedrock_input_data, indent=2, ensure_ascii=False)
    if is_first_of_month:
        # 1日実行時：前月の実績報告用のプロンプト
        human_prompt = f"""以下のコストデータを分析し、指定されたJSON形式で報告書を作成してください。

[データ]
{human_prompt_data_str}

[出力形式]
{{
  "summary": "（例：2025年7月1日から現在までの実績コストは$1500.00 (約225000円)でした。）",
  "risk_assessment": "（例：月次予算(10000円)を大幅に超過しました。特にAmazon EC2 (ap-northeast-1) のコストが$800.20(約120030円)と大半を占めています。）",
  "recommended_actions": "（例：ap-northeast-1のEC2インスタンスの利用状況を確認し、不要なリソースの停止を検討してください。）"
}}"""
    else:
        # 2日以降実行時：当月の途中経過報告用のプロンプト
        human_prompt = f"""以下のコストデータを分析し、指定されたJSON形式で報告書を作成してください。

[データ]
{human_prompt_data_str}

[出力形式]
{{
  "summary": "（例：2025年7月1日〜4日のコストは$7.59 (約1139円)で、月次予算額10,000円に対して予算を順調に消化しています。ただし、月次の予測コストが$0 (約0円)のため、現時点では予算超過のリスクは低い状況です）",
  "risk_assessment": "（例：コストの主な要因は、Amazon Elastic Compute Cloud - Compute (ap-northeast-1) が$1.22 (約183円)、Amazon ElastiCache (ap-northeast-1) が$1.05 (約158円)、EC2 - Other (ap-northeast-1) が$0.99 (約149円)となっています。現時点では月次予算を超過するリスクは低いと考えられます）",
  "recommended_actions": "（例：コストの主要因となっているリソースの利用状況を確認し、必要以上の利用がないかを確認することが重要です。また、CloudWatchアラームの設定などにより、予算超過を未然に防ぐことも検討する必要があります）"
}}"""

    try:
        logger.info("Invoking Bedrock for monthly cost analysis...")
        response = bedrock_client.invoke_model(modelId=BEDROCK_MODEL_ID, body=json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 1024, "messages": [{"role": "user", "content": [{"type": "text", "text": f"{system_prompt}\\n\\n{human_prompt}"}]}]}))
        response_body = json.loads(response['body'].read())
        raw_text = response_body.get('content', [{}])[0].get('text', '{}')
        analysis_result = json.loads(raw_text[raw_text.find("{"):raw_text.rfind("}") + 1])
        summary_event.update({f'analysis.{k}': v for k, v in analysis_result.items()})
        logger.info("Successfully added Bedrock analysis to summary.")
    except Exception as e:
        logger.error(f"Bedrock analysis FAILED: {e}", exc_info=True)
        summary_event['analysis.error'] = str(e)

    events.append(summary_event)
    logger.info(f"--- Finished Cost Explorer Workflow. Generated {len(events)} events. ---")
    return events


# =================================================================
# ワークフロー2: コスト削減推奨の処理 (変更なし)
# =================================================================
def run_recommendation_workflow(aws_account_id: str) -> List[Dict]:
    logger.info("--- Running Cost Recommendation Workflow ---")
    all_recommendations = []
    next_token = None
    while True:
        params = {'filter': {'accountIds': [aws_account_id]}, 'maxResults': 100}
        if next_token: params['nextToken'] = next_token
        response = coh_client.list_recommendations(**params)
        all_recommendations.extend(response.get('items', []))
        next_token = response.get('nextToken')
        if not next_token: break
    
    events = []
    if not all_recommendations:
        logger.info("No recommendations found.")
        return events
    total_savings = 0.0
    recommendations_by_type = {}
    bedrock_recommendation_summary_list = []
    for rec in all_recommendations:
        try:
            estimated_savings_usd = float(rec.get('estimatedMonthlySavings', 0))
            total_savings += estimated_savings_usd
            resource_type = str(rec.get('currentResourceType', 'Unknown'))
            recommendations_by_type[resource_type] = recommendations_by_type.get(resource_type, 0) + 1
            detail_event = {
                'eventType': 'AwsOptimizationReport', 'recordType': 'detail', 'aws.accountId': aws_account_id,
                'aws.region': str(rec.get('region', '')), 'aws.recommendationId': str(rec.get('recommendationId', '')),
                'aws.currentResourceType': resource_type, 'aws.currentResourceId': str(rec.get('resourceId', '')),
                'aws.currentResourceArn': str(rec.get('resourceArn', '')), 'aws.currentResourceSummary': str(rec.get('currentResourceSummary', '')),
                'aws.recommendedResourceSummary': str(rec.get('recommendedResourceSummary', '')), 'aws.implementationActionType': str(rec.get('actionType', '')),
                'aws.implementationEffort': str(rec.get('implementationEffort', '')), 'aws.analysisSource': str(rec.get('source', '')),
                'cost.estimatedMonthlySavings': estimated_savings_usd, 'cost.estimatedSavingsPercentage': float(rec.get('estimatedSavingsPercentage', 0))
            }
            events.append(detail_event)
            bedrock_recommendation_summary_list.append({
                "resourceType": resource_type, "action": str(rec.get('actionType', '')),
                "estimatedSavingsUsd": estimated_savings_usd,
                "estimatedSavingsJpy": round(estimated_savings_usd * JPY_EXCHANGE_RATE),
                "implementationEffort": str(rec.get('implementationEffort', ''))
            })
        except (ValueError, TypeError) as e:
            logger.error(f"Skipping a recommendation due to data format error: {e}. Data: {rec}")
            continue
    total_savings_jpy = round(total_savings * JPY_EXCHANGE_RATE)
    summary_event = {
        'eventType': 'AwsOptimizationReport', 'recordType': 'summary',
        'aws.accountId': aws_account_id, 'aws.lambdaRegion': TARGET_REGION,
        'recommendation.totalCount': len(all_recommendations),
        'cost.totalEstimatedSavings': total_savings,
        'cost.totalEstimatedSavingsJpy': total_savings_jpy,
        'recommendation.countByType': json.dumps(recommendations_by_type)
    }
    bedrock_input_data = {
        'totalRecommendations': summary_event.get('recommendation.totalCount'),
        'totalEstimatedSavingsUsd': summary_event.get('cost.totalEstimatedSavings'),
        'totalEstimatedSavingsJpy': summary_event.get('cost.totalEstimatedSavingsJpy'),
        'allRecommendationsSummary': bedrock_recommendation_summary_list
    }
    system_prompt = """あなたは経験豊富なクラウドコスト最適化コンサルタントです。提供されたAWSのコスト削減推奨事項のリストを分析し、IT管理者向けの実行可能なアクションプランをJSON形式で作成してください。
あなたのタスクは以下の通りです:
1. まず、全ての推奨事項をレビューし、全体の削減ポテンシャル（USDとJPY）を把握します。
2. 次に、各推奨事項の削減見込み額と `implementationEffort` を考慮し、優先順位を判断します。特に、「削減効果が大きく、手間が少ない」ものを最優先事項として特定します。
3. 上記分析に基づき、「全体の状況評価」「即時実行すべきアクション」「長期的・戦略的な推奨事項」の3つのパートに分けて、日本語で具体的なプランを提案してください。
4. 金額について言及する際は、データとして提供されているドル(USD)と日本円(JPY)の両方の値を併記してください。"""
    human_prompt = f"""以下のコスト削減推奨データを分析し、指定されたJSON形式でアクションプランを作成してください。

[データ]
{json.dumps(bedrock_input_data, default=json_serial_datetime, indent=2, ensure_ascii=False)}

[出力形式]
{{
  "overall_assessment": "（例：全体で7件の推奨があり、月間約$8.008 (約1201円) の削減ポテンシャルがあります。すべての推奨事項がEBSボリュームの削除に関するものであり、特に削減効果が大きく実装の手間も少ない提案が中心です。）",
  "immediate_actions": [
    {{
      "priority": "高",
      "action": "（例：EBSボリュームの定期的な棚卸しプロセスの導入や、アクティビティーに応じたEBSボリュームのサイズ最適化など、継続的なコスト最適化を目指すべきです。また、Savings Plansの適用検討も検討することで、長期的な観点でのコスト管理が可能となります）",
      "estimated_savings_usd": 150.75,
      "estimated_savings_jpy": 22612,
      "reason_for_priority": "削減効果が大きく、実装が容易なため。"
    }}
  ],
  "strategic_recommendation": "（例：定期的な棚卸しプロセスの導入や、Savings Plansの適用を検討することで、継続的なコスト最適化を目指すべきです。）"
}}"""
    try:
        logger.info("Invoking Bedrock for recommendation analysis...")
        response = bedrock_client.invoke_model(modelId=BEDROCK_MODEL_ID, body=json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 4096, "messages": [{"role": "user", "content": [{"type": "text", "text": f"{system_prompt}\\n\\n{human_prompt}"}]}]}))
        response_body = json.loads(response['body'].read())
        raw_text = response_body.get('content', [{}])[0].get('text', '{}')
        analysis_result = json.loads(raw_text[raw_text.find("{"):raw_text.rfind("}") + 1])
        summary_event.update({f'analysis.{k}': v for k, v in analysis_result.items()})
        logger.info("Successfully added Bedrock analysis to recommendation summary.")
    except Exception as e:
        logger.error(f"Bedrock analysis for recommendations FAILED: {e}", exc_info=True)
        summary_event['analysis.error'] = str(e)
    events.append(summary_event)
    logger.info(f"--- Finished Cost Recommendation Workflow. Generated {len(events)} events. ---")
    return events

# =================================================================
# New Relicへのデータ送信 (変更なし)
# =================================================================
def send_to_new_relic(events: List[Dict]) -> None:
    if not events: logger.info("No events to send."); return
    gzipped_payload = gzip.compress(json.dumps(events, default=json_serial_datetime).encode('utf-8'))
    headers = {'Content-Type': 'application/json', 'Api-Key': NEW_RELIC_LICENSE_KEY, 'Content-Encoding': 'gzip'}
    endpoint = 'insights-collector.eu01.nr-data.net' if NEW_RELIC_LICENSE_KEY.startswith('eu') else 'insights-collector.newrelic.com'
    url = f'/v1/accounts/{NEW_RELIC_ACCOUNT_ID}/events'
    for attempt in range(3):
        try:
            connection = http.client.HTTPSConnection(endpoint, timeout=15)
            connection.request('POST', url, gzipped_payload, headers)
            response = connection.getresponse()
            if 200 <= response.status < 300:
                logger.info(f"Successfully sent {len(events)} events to New Relic.")
                return
            logger.warning(f"New Relic send attempt {attempt + 1} failed with status {response.status}. Retrying...")
        except Exception as e:
            logger.warning(f"New Relic send attempt {attempt + 1} failed with error: {e}.")
        finally:
            if 'connection' in locals(): connection.close()
        time.sleep((1 * 2 ** attempt) + random.uniform(0, 1))
    raise Exception("Failed to send events to New Relic after all retries.")

# =================================================================
# Lambdaハンドラー (変更なし)
# =================================================================
def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    if not NEW_RELIC_LICENSE_KEY or not NEW_RELIC_ACCOUNT_ID:
        logger.error("Missing New Relic credentials in environment variables.")
        return {'statusCode': 500, 'body': 'Missing New Relic configuration.'}
    logger.info("Monolithic Lambda execution started.")
    aws_account_id = context.invoked_function_arn.split(":")[4]
    all_events_to_send = []
    try:
        cost_events = run_cost_explorer_workflow(aws_account_id)
        all_events_to_send.extend(cost_events)
    except Exception as e:
        logger.error(f"Cost Explorer workflow encountered an unhandled error: {e}", exc_info=True)
    try:
        rec_events = run_recommendation_workflow(aws_account_id)
        all_events_to_send.extend(rec_events)
    except Exception as e:
        logger.error(f"Cost Recommendation workflow encountered an unhandled error: {e}", exc_info=True)
    if all_events_to_send:
        try:
            send_to_new_relic(all_events_to_send)
        except Exception as e:
            logger.error(f"Final send to New Relic failed: {e}", exc_info=True)
            raise e
    else:
        logger.warning("No events were generated from any workflow.")
    logger.info("Monolithic Lambda execution finished successfully.")
    return {'statusCode': 200, 'body': json.dumps({'message': f'Processing complete. Total events generated: {len(all_events_to_send)}'})}