resource "example_queue" "orders" {
  name                       = "orders"
  message_retention_days     = 7
  visibility_timeout_seconds = 45
  max_receive_count          = 5
  dead_letter_queue          = example_queue.orders_dlq.id
}

resource "example_queue" "orders_dlq" {
  name                   = "orders-dlq"
  message_retention_days = 14
}
