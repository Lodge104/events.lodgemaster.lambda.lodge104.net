# lodgemaster-eventdata

Syncs every Lodge 104 event from the LodgeMaster portal into DynamoDB on a
schedule. Infrastructure is managed with Terraform (`terraform/`).

## How it works

```
lodge104-lodgemaster-scrapper (separate repo/stack)
        │ refreshes session cookies
        ▼
lodge104-keys (DynamoDB, owned by the scrapper)
        │
        ▼
lodge104-eventdata (this Lambda) ◀── EventBridge schedule (rate(1 hour))
        │  POST /api/app/login
        │  GET  /api/events/            (list of all events)
        │  GET  /api/events/{id}        (details, once per event)
        ▼
lodge104-event_data (DynamoDB, owned by this repo, PK: id)
```

1. Reads every cookie in `lodge104-keys` and calls `POST /api/app/login` to
   obtain an authorised API session (same flow as the member-data function).
2. Fetches the event list from `GET /api/events/`.
3. For each event, fetches `GET /api/events/{id}` (concurrently, controlled
   by `DETAIL_FETCH_CONCURRENCY`) and merges it into the list record. Where
   fields overlap, the details value wins. Each item also gets a
   `lastSyncedAt` UTC timestamp.
4. Writes all records with `batch_writer()` and deletes items whose `id` no
   longer appears in LodgeMaster. If the list comes back empty the run fails
   rather than wiping the table.

Each item therefore contains list-only fields (`type`, `chapter`,
`eventLocation`, `countRegistered`, `countCheckedIn`, `paidAmount`, ...) and
detail-only fields (`eventTypeId`, `eventLocationId`, `notes`,
`postEventReport`, `has*` flags, `isOnlineRegEnabled`,
`onlineRegStartDate`/`EndDate`, `onlineRegCapacity`, registration URLs, ...).

## Infrastructure (`terraform/`)

| Resource | Notes |
| --- | --- |
| `aws_dynamodb_table.event_data` | `lodge104-event_data`, on-demand, PK `id` (N), PITR, deletion protection, `prevent_destroy` |
| `aws_lambda_function.event_sync` | `lodge104-eventdata`, python3.12/arm64, reserved concurrency 1, no async retries |
| `aws_iam_role.lambda` + inline policy | Logs to its own log group, `Scan` on `lodge104-keys`, `Scan`/`BatchWriteItem`/`PutItem`/`DeleteItem` on the event table |
| `aws_cloudwatch_event_rule.schedule` | `var.schedule_expression` (default hourly) |
| `aws_cloudwatch_log_group.lambda` | 30-day retention |
| `aws_cloudwatch_metric_alarm.errors` | Publishes ALARM/OK to the shared `lodge104-lambda` SNS topic |

`lodge104-keys` and the `lodge104-lambda` SNS topic are **referenced only**,
never created or managed here.

## Deploying

State is stored in S3 using a partial backend config:

```bash
cd terraform
cp backend.hcl.example backend.hcl   # set the state bucket
terraform init -backend-config=backend.hcl
terraform plan
terraform apply
```

The Lambda zip is built from `src/` by the `archive` provider during
`plan`/`apply`; there are no third-party dependencies (boto3/urllib3 come
from the Lambda runtime).

Run a sync manually:

```bash
aws lambda invoke --function-name lodge104-eventdata /dev/stdout
```

## Notes

- `getevents.txt` / `geteventdetails.txt` are captured browser requests that
  contain live session cookies; they are git-ignored and should not be
  committed.
- CI runs pylint on `src/` and `terraform fmt`/`validate` on `terraform/`.
