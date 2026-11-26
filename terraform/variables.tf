variable "region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "function_name" {
  description = "Name of the event sync Lambda function."
  type        = string
  default     = "lodge104-eventdata"
}

variable "event_data_table_name" {
  description = "DynamoDB table that stores LodgeMaster events. Owned by this configuration."
  type        = string
  default     = "lodge104-event_data"
}

variable "keys_table_name" {
  description = <<-EOT
    Existing DynamoDB table holding the LodgeMaster portal session cookies,
    refreshed by the separately-owned lodge104-lodgemaster-scrapper function.
    Referenced by name only; never created or managed here.
  EOT
  type        = string
  default     = "lodge104-keys"
}

variable "lodgemaster_base_url" {
  description = "Base URL of the LodgeMaster portal."
  type        = string
  default     = "https://lodgemaster-client.oa-scouting.org"
}

variable "schedule_expression" {
  description = "EventBridge schedule expression for the sync."
  type        = string
  default     = "rate(1 hour)"
}

variable "detail_fetch_concurrency" {
  description = "Number of concurrent GET /api/events/{id} requests."
  type        = number
  default     = 8
}

variable "lambda_memory_size" {
  description = "Lambda memory in MB."
  type        = number
  default     = 512
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds."
  type        = number
  default     = 300
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the function's log group."
  type        = number
  default     = 30
}

variable "alarm_topic_arn" {
  description = <<-EOT
    ARN of the existing, shared SNS topic that receives operational alerts.
    Referenced by ARN only; not managed here. Set to "" to skip alarm actions.
  EOT
  type        = string
  default     = "arn:aws:sns:us-east-1:423971488961:lodge104-lambda"
}

variable "error_alarm_threshold" {
  description = "Errors within the evaluation window that trigger the alarm."
  type        = number
  default     = 1
}

variable "error_alarm_period_seconds" {
  description = "Length, in seconds, of each alarm evaluation period."
  type        = number
  default     = 3600
}

variable "error_alarm_evaluation_periods" {
  description = "Number of periods evaluated by the alarm."
  type        = number
  default     = 3
}

variable "error_alarm_datapoints_to_alarm" {
  description = "Breaching datapoints, out of the evaluation periods, required to alarm."
  type        = number
  default     = 2
}
