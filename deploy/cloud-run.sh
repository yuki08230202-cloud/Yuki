#!/usr/bin/env bash
# Cloud Run + Cloud SQL(PostgreSQL) + Cloud Scheduler + Secret Manager へのデプロイ例。
# 事前に: gcloud auth login / gcloud config set project $PROJECT
# 秘密情報は Secret Manager に登録してから実行する（下の SECRETS を参照）。
set -euo pipefail

PROJECT=${PROJECT:?PROJECT を指定}
REGION=${REGION:-asia-northeast1}
SERVICE=${SERVICE:-enpra}
SQL_INSTANCE=${SQL_INSTANCE:-enpra-db}
SA=${SA:-enpra-run@${PROJECT}.iam.gserviceaccount.com}

# --- 初回のみ: Cloud SQL（最小構成・自動バックアップ有効） ---
if ! gcloud sql instances describe "$SQL_INSTANCE" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud sql instances create "$SQL_INSTANCE" --project "$PROJECT" --region "$REGION" \
    --database-version POSTGRES_16 --edition ENTERPRISE --tier db-f1-micro \
    --storage-size 10 --backup-start-time 18:00 --enable-point-in-time-recovery
  gcloud sql databases create enpra --instance "$SQL_INSTANCE" --project "$PROJECT"
fi

SECRETS="DATABASE_URL=enpra-database-url:latest,CHATWORK_API_TOKEN=enpra-chatwork-token:latest,\
CHATWORK_NOTIFY_TOKEN=enpra-chatwork-notify-token:latest,ADMIN_PASSWORD_HASH=enpra-admin-password-hash:latest,\
SESSION_SECRET=enpra-session-secret:latest,JOB_SECRET=enpra-job-secret:latest,SMTP_PASSWORD=enpra-smtp-password:latest,\
CHATWORK_WEBHOOK_TOKEN=enpra-chatwork-webhook-token:latest"

gcloud run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" --source . \
  --service-account "$SA" --allow-unauthenticated \
  --min-instances 0 --max-instances 1 --memory 512Mi --timeout 300 \
  --add-cloudsql-instances "${PROJECT}:${REGION}:${SQL_INSTANCE}" \
  --set-secrets "$SECRETS" \
  --env-vars-file deploy/env.yaml

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format 'value(status.url)')
JOB_SECRET=$(gcloud secrets versions access latest --secret enpra-job-secret --project "$PROJECT")

# --- Cloud Scheduler（JST）。19:00 の募集送信は時刻厳守のため分をずらさない ---
schedule() {  # name cron
  local name=$1 cron=$2
  local args=(--project "$PROJECT" --location "$REGION" --schedule "$cron" --time-zone "Asia/Tokyo"
    --uri "$URL/jobs/$name" --http-method POST --headers "X-Job-Secret=$JOB_SECRET"
    --attempt-deadline 300s --max-retry-attempts 3 --min-backoff 30s)
  if gcloud scheduler jobs describe "enpra-$name" --project "$PROJECT" --location "$REGION" >/dev/null 2>&1; then
    gcloud scheduler jobs update http "enpra-$name" "${args[@]}"
  else
    gcloud scheduler jobs create http "enpra-$name" "${args[@]}"
  fi
}
schedule recruit-prepare "30 18 * * *"
schedule recruit-send    "0 19 * * *"
schedule participation   "0 22 * * *"
schedule daily-report    "0 7 * * *"
schedule invoices        "0 21 * * *"
schedule contacts-check  "55 8,20 * * *"
schedule onboarding      "50 9 * * *"
schedule indeed          "45 9 * * *"
schedule qa-poll         "*/5 * * * *"
schedule maintenance     "*/5 * * * *"
echo "deployed: $URL"
