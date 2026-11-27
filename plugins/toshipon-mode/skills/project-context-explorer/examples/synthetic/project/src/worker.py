"""Synthetic example: the worker that consumes the orders queue."""

MAX_PROCESSING_SECONDS = 40  # the slowest order seen in the synthetic load test


def handle(message, ledger):
    """Charge the order once. A redelivered message is charged again unless the ledger knows it."""
    if ledger.seen(message.id):
        return "duplicate"
    ledger.charge(message.order_id, message.amount)
    ledger.mark(message.id)
    return "charged"
