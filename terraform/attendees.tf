# -----------------------------------------------------------------------------
# Event attendees sync: every 5 minutes, mirrors the registrations of every
# upcoming/in-progress event (read from the event_data table) into a
# separate attendees table. Shares the code bundle and the scheduler role
# with the event sync in main.tf.
# -----------------------------------------------------------------------------

# One item per (event, registration). A member registered for several events
# has one item per event; the BsaPersonId-index finds all of them.
resource "aws_dynamodb_table" "event_attendees" {
  name                        = var.event_attendees_table_name
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "eventId"
  range_key                   = "EventMemberId"
  deletion_protection_enabled = true

  attribute {
    name = "eventId"
    type = "N"
  }

  attribute {
    name = "EventMemberId"
    type = "N"
  }

  attribute {
    name = "BsaPersonId"
    type = "N"
  }

  global_secondary_index {
    name            = "BsaPersonId-index"
    projection_type = "ALL"

    key_schema {
      attribute_name = "BsaPersonId"
      key_type       = "HASH"
    }

    key_schema {
      attribute_name = "eventId"
      key_type       = "RANGE"
    }
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
# Execution role
# -----------------------------------------------------------------------------
resource "aws_iam_role" "attendees_lambda" {
  name               = "${var.attendees_function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "attendees_lambda" {
  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.attendees_lambda.arn}:*"]
  }

  statement {
    sid       = "ReadSessionCookies"
    actions   = ["dynamodb:Scan"]
    resources = [local.keys_table_arn]
  }

  # Scan the events table to find upcoming events.
  statement {
    sid       = "ReadEventData"
    actions   = ["dynamodb:Scan"]
    resources = [aws_dynamodb_table.event_data.arn]
  }

  # Query existing attendees per event; put changed / delete removed ones.
  statement {
    sid = "SyncEventAttendees"
    actions = [
      "dynamodb:Query",
      "dynamodb:BatchWriteItem",
      "dynamodb:PutItem",
      "dynamodb:DeleteItem",
    ]
    resources = [aws_dynamodb_table.event_attendees.arn]
  }
}

resource "aws_iam_role_policy" "attendees_lambda" {
  name   = "${var.attendees_function_name}-policy"
  role   = aws_iam_role.attendees_lambda.id
  policy = data.aws_iam_policy_document.attendees_lambda.json
}

# -----------------------------------------------------------------------------
# Lambda function
# -----------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "attendees_lambda" {
  name              = "/aws/lambda/${var.attendees_function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "attendees_sync" {
  function_name    = var.attendees_function_name
  description      = "Syncs LodgeMaster attendees of upcoming events into DynamoDB."
  role             = aws_iam_role.attendees_lambda.arn
  handler          = "attendees_function.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  memory_size      = var.attendees_lambda_memory_size
  timeout          = var.attendees_lambda_timeout
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  # Runs every 5 minutes; never let a slow run overlap the next one.
  reserved_concurrent_executions = 1

  environment {
    variables = {
      EVENT_DATA_TABLE_NAME      = aws_dynamodb_table.event_data.name
      EVENT_ATTENDEES_TABLE_NAME = aws_dynamodb_table.event_attendees.name
      KEYS_TABLE_NAME            = var.keys_table_name
      LODGEMASTER_BASE_URL       = var.lodgemaster_base_url
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.attendees_lambda.name
  }

  depends_on = [aws_iam_role_policy.attendees_lambda]
}

# The next run is at most 5 minutes away, so retries only add load.
resource "aws_lambda_function_event_invoke_config" "attendees_sync" {
  function_name          = aws_lambda_function.attendees_sync.function_name
  maximum_retry_attempts = 0
}

# -----------------------------------------------------------------------------
# Schedule
# -----------------------------------------------------------------------------
resource "aws_scheduler_schedule" "attendees_sync" {
  name                = "${var.attendees_function_name}-schedule"
  description         = "Runs ${var.attendees_function_name} every 5 minutes."
  schedule_expression = var.attendees_schedule_expression
  state               = "ENABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.attendees_sync.arn
    role_arn = aws_iam_role.scheduler.arn

    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}

# -----------------------------------------------------------------------------
# Error alarm -> shared SNS topic. Unlike the twice-daily event sync, a single
# failed 5-minute run is noise (e.g. a transient LodgeMaster blip), so this
# alarms when failures persist for most of an hour.
# -----------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "attendees_errors" {
  alarm_name          = "AWS/Lambda Errors FunctionName=${var.attendees_function_name}"
  alarm_description   = <<-EOT
    ${var.attendees_function_name} has been failing to sync event attendees
    into ${var.event_attendees_table_name}. Check the function's CloudWatch
    Logs. The most common cause is expired LodgeMaster session cookies in
    ${var.keys_table_name} (lodge104-lodgemaster-scrapper not running).
  EOT
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 4
  datapoints_to_alarm = 3
  threshold           = var.attendees_error_alarm_threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  dimensions = {
    FunctionName = aws_lambda_function.attendees_sync.function_name
  }
}
