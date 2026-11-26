data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id      = data.aws_caller_identity.current.account_id
  partition       = data.aws_partition.current.partition
  keys_table_arn  = "arn:${local.partition}:dynamodb:${var.region}:${local.account_id}:table/${var.keys_table_name}"
  alarm_actions   = var.alarm_topic_arn == "" ? [] : [var.alarm_topic_arn]
  lambda_src_path = "${path.module}/../src"
}

# -----------------------------------------------------------------------------
# DynamoDB table holding every LodgeMaster event, keyed by its LodgeMaster id.
# -----------------------------------------------------------------------------
resource "aws_dynamodb_table" "event_data" {
  name                        = var.event_data_table_name
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "id"
  deletion_protection_enabled = true

  attribute {
    name = "id"
    type = "N"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  lifecycle {
    prevent_destroy = true
  }
}

# -----------------------------------------------------------------------------
# Lambda execution role
# -----------------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda.arn}:*"]
  }

  # The function forwards every cookie lodge104-lodgemaster-scrapper has
  # written to this table; Scan has no per-item scoping, so this is scoped
  # to the table ARN.
  statement {
    sid       = "ReadSessionCookies"
    actions   = ["dynamodb:Scan"]
    resources = [local.keys_table_arn]
  }

  # Scan: list existing ids to find stale events.
  # BatchWriteItem: put/delete events via batch_writer().
  statement {
    sid = "SyncEventData"
    actions = [
      "dynamodb:Scan",
      "dynamodb:BatchWriteItem",
      "dynamodb:PutItem",
      "dynamodb:DeleteItem",
    ]
    resources = [aws_dynamodb_table.event_data.arn]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${var.function_name}-policy"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

# -----------------------------------------------------------------------------
# Lambda function
# -----------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.function_name}"
  retention_in_days = var.log_retention_days
}

data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = local.lambda_src_path
  output_path = "${path.module}/.build/${var.function_name}.zip"
  excludes    = ["__pycache__", "requirements.txt"]
}

resource "aws_lambda_function" "event_sync" {
  function_name    = var.function_name
  description      = "Syncs LodgeMaster events (enriched with event details) into DynamoDB."
  role             = aws_iam_role.lambda.arn
  handler          = "lambda_function.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  memory_size      = var.lambda_memory_size
  timeout          = var.lambda_timeout
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  # Only one sync should run at a time.
  reserved_concurrent_executions = 1

  environment {
    variables = {
      EVENT_DATA_TABLE_NAME    = aws_dynamodb_table.event_data.name
      KEYS_TABLE_NAME          = var.keys_table_name
      LODGEMASTER_BASE_URL     = var.lodgemaster_base_url
      DETAIL_FETCH_CONCURRENCY = tostring(var.detail_fetch_concurrency)
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.lambda.name
  }

  depends_on = [aws_iam_role_policy.lambda]
}

# Scheduled runs are not retried: the next run resyncs everything anyway.
resource "aws_lambda_function_event_invoke_config" "event_sync" {
  function_name          = aws_lambda_function.event_sync.function_name
  maximum_retry_attempts = 0
}

# -----------------------------------------------------------------------------
# Schedule
# -----------------------------------------------------------------------------
resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "${var.function_name}-schedule"
  description         = "Triggers ${var.function_name} to sync LodgeMaster events."
  schedule_expression = var.schedule_expression
}

resource "aws_cloudwatch_event_target" "schedule" {
  rule = aws_cloudwatch_event_rule.schedule.name
  arn  = aws_lambda_function.event_sync.arn
}

resource "aws_lambda_permission" "schedule" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.event_sync.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}

# -----------------------------------------------------------------------------
# Error alarm -> shared SNS topic
# -----------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "AWS/Lambda Errors FunctionName=${var.function_name}"
  alarm_description   = <<-EOT
    ${var.function_name} failed to sync LodgeMaster events into
    ${var.event_data_table_name}. Check the function's CloudWatch Logs. The
    most common cause is expired LodgeMaster session cookies in
    ${var.keys_table_name} (lodge104-lodgemaster-scrapper not running).
  EOT
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = var.error_alarm_period_seconds
  evaluation_periods  = var.error_alarm_evaluation_periods
  datapoints_to_alarm = var.error_alarm_datapoints_to_alarm
  threshold           = var.error_alarm_threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  dimensions = {
    FunctionName = aws_lambda_function.event_sync.function_name
  }
}
