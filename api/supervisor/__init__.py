"""Order Supervisor — long-running AI supervision of a single order."""

TASK_QUEUE = "order-supervisor"


def workflow_id_for(order_id: str) -> str:
    """Deterministic id => exactly one workflow execution per order."""
    return f"order-{order_id}"
