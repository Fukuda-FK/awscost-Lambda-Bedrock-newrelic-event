# AI-Powered AWS FinOps Bot for New Relic

このリポジトリは、AWSのコスト監視と最適化を自動化するために設計されたLambda関数のコードを含んでいます。この関数は、AWS Cost Explorerからコストデータを、AWS Cost Optimization Hubから推奨事項を取得し、Amazon BedrockによるAI分析でインサイトと実行計画を生成します。そして、すべてのデータをNew Relicへカスタムイベントとして送信し、一元的な可視化とアラートを実現します。

## ✨ 主な機能

  - **月次コストの自動レポーティング**: AWS Cost Explorerから、指定したディメンションやタグでグループ化された月次コストデータを自動で取得します。
  - **AIによるコスト分析**: Amazon Bedrock（Anthropic Claude 3 Haiku）を利用して、コスト傾向の分析、予算超過リスクの評価、そして日本語による状況の要約を生成します。
  - **コスト削減推奨事項の自動分析**: AWS Cost Optimization Hubからコスト削減に関する推奨事項を自動で取得します。
  - **AIによるアクションプラン生成**: Amazon Bedrockを活用し、取得した推奨事項を分析して、優先順位付けされた具体的なコスト削減アクションプランを日本語で生成します。
  - **New Relicとの連携**: 詳細データとAIによる分析結果を含むサマリーデータを、カスタムイベント（`AwsCostReport`, `AwsOptimizationReport`）としてNew Relicに送信します。
  - **柔軟な設定**: Lambdaの環境変数を通じて、グループ化のキーや利用するAIモデルなどを簡単に設定変更できます。

-----

## 🏗️ アーキテクチャ

アーキテクチャはシンプルでサーバーレスな構成です。Amazon EventBridgeのスケジュール機能がLambda関数を定期的に（例：毎日）トリガーします。関数は各種AWSサービスと連携し、処理結果をNew Relicに送信します。

```
┌───────────────┐      ┌──────────────┐      ┌───────────────────────────┐      ┌───────────┐
│ Amazon        ├─────►│  AWS Lambda  ├─────►│ AWS Cost Explorer         ├─────►│           │
│ EventBridge   │      │              │      │ AWS Cost Optimization Hub │      │ New Relic │
│ (スケジューラ)   │      │ (このコード)    │      │ Amazon Bedrock            │      │           │
└───────────────┘      └──────────────┘      └───────────────────────────┘      └───────────┘
```

-----

## ⚙️ セットアップとデプロイ手順

### 1\. 前提条件

  - 有効な**AWSアカウント**
  - **New Relicアカウント**、およびその**License Key**と**Account ID**
  - **Amazon Bedrockのモデルアクセス**: 利用したい`BEDROCK_REGION`のAWSコンソールで、使用するモデル（例: `anthropic.claude-3-haiku-20240307-v1:0`）へのアクセスを事前にリクエストし、有効化しておく必要があります。

### 2\. Lambda用IAMロールの作成

Lambda関数が各種AWSサービスにアクセスするために、以下の権限を持つIAMロールを作成します。

**IAMポリシー (JSON):**

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "LambdaLogging",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents"
            ],
            "Resource": "arn:aws:logs:*:*:*"
        },
        {
            "Sid": "CostExplorerAndOptimizationHub",
            "Effect": "Allow",
            "Action": [
                "ce:GetCostAndUsage",
                "ce:GetCostForecast",
                "cost-optimization-hub:ListRecommendations"
            ],
            "Resource": "*"
        },
        {
            "Sid": "BedrockInvokeModel",
            "Effect": "Allow",
            "Action": "bedrock:InvokeModel",
            "Resource": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-haiku-20240307-v1:0"
        }
    ]
}
```

> **注意**: `bedrock:InvokeModel`の`Resource`部分は、実際に利用する`BEDROCK_MODEL_ID`と`BEDROCK_REGION`に合わせて修正してください。

### 3\. Lambda関数の作成

1.  AWS Lambdaのコンソールに移動します。
2.  **[関数の作成]** をクリックします。
3.  **[一から作成]** を選択します。
4.  **関数名**: `FinOpsBotForNewRelic` など、わかりやすい名前を入力します。
5.  **ランタイム**: **Python 3.11** 以降を選択します。
6.  **アーキテクチャ**: `x86_64` を選択します。
7.  **アクセス権限**: **[既存のロールを使用する]** を選択し、ステップ2で作成したIAMロールをアタッチします。
8.  **[関数の作成]** をクリックします。

### 4\. コードのデプロイ

1.  このリポジトリにある`lambda_function.py`（ファイル名を変更した場合はそのファイル）を準備します。
2.  依存ライブラリはないため、Pythonファイル単体を`zip`ファイルに圧縮します。
3.  作成したLambda関数の **[コード]** タブで、**[アップロード元]** をクリックし、**[.zip ファイル]** を選択して圧縮したファイルをアップロードします。

### 5\. 環境変数の設定

Lambda関数の **[設定]** \> **[環境変数]** で、以下のキーと値を設定します。

| キー | 説明 | 設定例 |
| :--- | :--- | :--- |
| `NEW_RELIC_LICENSE_KEY` | **(必須)** New RelicのIngest License Key。 | `NR...` |
| `NEW_RELIC_ACCOUNT_ID` | **(必須)** New RelicのアカウントID。 | `1234567` |
| `TARGET_REGION` | Cost ExplorerとCost Optimization Hubが利用するAWSリージョン。 | `us-east-1` |
| `GROUP_BY_DIMENSION_KEY`| コストをグループ化する際のDimensionキー。`SERVICE`や`USAGE_TYPE`など。 | `SERVICE` |
| `GROUP_BY_TAG_KEY` | (任意) コストをグループ化する際のタグキー。 | `Project` |
| `BEDROCK_MODEL_ID` | AI分析に使用するBedrockのモデルID。 | `anthropic.claude-3-haiku-20240307-v1:0` |
| `BEDROCK_REGION` | Bedrockモデルがホストされているリージョン。 | `us-east-1` |
| `JPY_EXCHANGE_RATE` | USDをJPYに換算する際のレート。 | `150` |

### 6\. トリガーの設定

1.  Lambda関数のデザイナー画面で **[トリガーを追加]** をクリックします。
2.  トリガーとして **EventBridge (CloudWatch Events)** を選択します。
3.  **ルール**: **[新規ルールを作成]** を選択します。
4.  **ルール名**: `DailyFinOpsTrigger` など、わかりやすい名前を入力します。
5.  **スケジュール式**: `rate(1 day)` や `cron(0 1 * * ? *)`（毎日午前1時 UTC）のように、実行したいスケジュールを設定します。
6.  **[追加]** をクリックしてトリガーを保存します。

-----

## 🔬 ワークフローロジック

この関数には2つの主要なワークフローがあります。

### 1\. コスト実績の処理 (`run_cost_explorer_workflow`)

  - **実行日によるロジック分岐**:
      - **毎月1日に実行された場合**: 前月（1日〜末日）全体のコスト実績データを取得し、報告書を生成します。
      - **毎月2日以降に実行された場合**: 当月の月初から実行日の前日までのコスト実績データを取得します。さらに、当月末までのコスト予測も取得し、途中経過報告書を生成します。
  - **AI分析**: 取得したコストデータ（実績、予測、コスト上位サービス）を基に、Bedrockが「状況の要約」「潜在的リスク」「推奨アクション」を含む日本語のレポートをJSON形式で生成します。

### 2\. コスト削減推奨の処理 (`run_recommendation_workflow`)

  - **データ取得**: AWS Cost Optimization Hubから、アカウント内のすべてのコスト削減推奨事項を取得します。
  - **AI分析**: 取得した推奨事項のリスト（リソースタイプ、削減見込み額、実装の労力など）を基に、Bedrockが「全体の状況評価」「即時実行すべきアクション」「長期的・戦略的な推奨事項」を含む、優先順位付けされた日本語のアクションプランをJSON形式で生成します。

-----

## 📊 New Relicへの出力

関数は処理結果をNew Relicにカスタムイベントとして送信します。NRQLを使ってクエリやダッシュボード作成が可能です。

  - `eventType`: **`AwsCostReport`**
      - `recordType`: `detail`（グループごとのコスト）または `summary`（全体のコストとAI分析結果）。
      - `analysis.summary`, `analysis.risk_assessment`, `analysis.recommended_actions`: AIが生成したコスト分析レポート。
  - `eventType`: **`AwsOptimizationReport`**
      - `recordType`: `detail`（個別の推奨事項）または `summary`（全体の推奨事項数とAI分析結果）。
      - `analysis.overall_assessment`, `analysis.immediate_actions`, `analysis.strategic_recommendation`: AIが生成したアクションプラン。

**NRQLクエリの例:**

```nrql
FROM AwsCostReport SELECT `analysis.summary`, `cost.totalUnblended` WHERE recordType = 'summary' SINCE 1 day ago
```

-----

## 🔧 カスタマイズ

この関数の挙動は、主に**環境変数**を変更することでカスタマイズできます。

  - コストのグループ化単位を変更したい場合は、`GROUP_BY_DIMENSION_KEY`や`GROUP_BY_TAG_KEY`の値を変更します。
  - 別のAIモデルを使用したい場合は、`BEDROCK_MODEL_ID`と`BEDROCK_REGION`を更新し、IAMポリシーも合わせて修正してください。
  - AIへの指示（プロンプト）を変更したい場合は、コード内の`system_prompt`や`human_prompt`の文字列を直接編集します。

-----