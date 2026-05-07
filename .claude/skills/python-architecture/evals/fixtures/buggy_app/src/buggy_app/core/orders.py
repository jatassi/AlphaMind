"""Order domain — internal types, business rules."""
from dataclasses import dataclass
from datetime import datetime
from pydantic import BaseModel
from buggy_app.storage.repo import save_order  # creates a cycle with storage.repo


# L8: mutable dataclass (no frozen=True)
@dataclass
class Order:
    order_id: int  # L: bare int as ID (primitive obsession)
    symbol: str
    quantity: int
    price: float        # L12: float for money
    submitted_at: datetime  # populated naively below
    status: str  # L: bare str as status (should be enum/Literal)


# L9: Pydantic BaseModel for an internal domain object
class Position(BaseModel):
    symbol: str
    quantity: int
    cost_basis: float  # also L12 indirectly


def submit_order(symbol: str, quantity: int, price: float) -> Order:
    """Submit an order. Returns the created Order."""
    order = Order(
        order_id=_next_id(),
        symbol=symbol,
        quantity=quantity,
        price=price,
        submitted_at=datetime.now(),  # L11: naive datetime
        status="submitted",
    )
    try:
        save_order(order)
    except Exception:  # L4: broad except outside outermost supervisor
        pass
    return order


_id_counter = 0  # L23-adjacent: module-level mutable global


def _next_id() -> int:
    global _id_counter
    _id_counter += 1
    return _id_counter
