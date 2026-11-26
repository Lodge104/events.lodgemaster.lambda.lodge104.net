output "function_name" {
  description = "Name of the event sync Lambda function."
  value       = aws_lambda_function.event_sync.function_name
}

output "function_arn" {
  description = "ARN of the event sync Lambda function."
  value       = aws_lambda_function.event_sync.arn
}

output "role_arn" {
  description = "ARN of the Lambda execution role."
  value       = aws_iam_role.lambda.arn
}

output "event_data_table_name" {
  description = "Name of the DynamoDB table holding LodgeMaster events."
  value       = aws_dynamodb_table.event_data.name
}

output "event_data_table_arn" {
  description = "ARN of the DynamoDB table holding LodgeMaster events."
  value       = aws_dynamodb_table.event_data.arn
}
