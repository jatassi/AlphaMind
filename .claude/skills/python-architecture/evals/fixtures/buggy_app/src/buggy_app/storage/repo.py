"""Storage adapter."""
# Import inside function to "avoid" the cycle (planted: L6 late-import-as-cycle-workaround)
def save_order(order) -> None:
    from buggy_app.core.orders import Order  # L6 — late import to dodge a real cycle
    print(f"saved {order.order_id}")  # L25 print in non-CLI
