"""Order/fill reconciliation against a venue's view of the world.

Motivation
----------
An order-management layer that only credits fills on orders it believes are
open will drift away from the venue's truth. The failure modes this module
guards against are all ones I have hit operating live systems:

* **Post-cancel fill races.** A TTL cancel and a fill can pass each other on
  the wire: the venue fills the order, then acknowledges the cancel (or the
  cancel ack arrives first and the fill report later). If the tracker drops
  the order at cancel time, the fill lands on nothing and position drifts.
* **Unknown fills.** Fills reported for order ids the tracker has never seen
  (restarts, manual intervention, a second process on the same account).
* **Position drift.** Even with every fill classified, the only ground truth
  is the venue's reported position; internal position must be reconciled
  against it periodically rather than assumed.

The design principle is *never silently absorb a discrepancy*: every venue
fill is classified, and reconciliation produces an explicit report that the
caller can alert on, rather than a corrected number that hides the problem.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class OrderState(Enum):
    SUBMITTED = "submitted"          # sent, not yet acknowledged
    OPEN = "open"                    # acknowledged by venue
    CANCEL_SENT = "cancel_sent"      # cancel in flight; fills still possible
    CANCELLED = "cancelled"          # cancel acknowledged; late fills still possible
    FILLED = "filled"


class FillClass(Enum):
    TRACKED = "tracked"              # fill on an order we consider live
    POST_CANCEL = "post_cancel"      # fill on an order we tried to cancel
    DUPLICATE = "duplicate"          # fill id already processed
    UNKNOWN = "unknown"              # fill on an order we have no record of


@dataclass
class TrackedOrder:
    order_id: str
    side: str                        # "buy" | "sell"
    price: float
    size: float
    state: OrderState = OrderState.SUBMITTED
    filled: float = 0.0
    cancel_sent_at: Optional[float] = None
    cancelled_at: Optional[float] = None


@dataclass
class ClassifiedFill:
    fill_id: str
    order_id: str
    side: str
    price: float
    size: float
    classification: FillClass


@dataclass
class ReconciliationReport:
    internal_position: float
    venue_position: float
    post_cancel_fills: List[ClassifiedFill] = field(default_factory=list)
    unknown_fills: List[ClassifiedFill] = field(default_factory=list)

    @property
    def position_drift(self) -> float:
        return self.venue_position - self.internal_position

    @property
    def clean(self) -> bool:
        return (
            abs(self.position_drift) < 1e-12
            and not self.post_cancel_fills
            and not self.unknown_fills
        )


class FillReconciler:
    """Tracks order lifecycle and classifies every fill the venue reports.

    Orders that reach a terminal state are retained for ``retention_seconds``
    so that late fill reports can still be matched to them instead of being
    misclassified as unknown.
    """

    def __init__(self, retention_seconds: float = 300.0) -> None:
        self._orders: Dict[str, TrackedOrder] = {}
        self._seen_fill_ids: set[str] = set()
        self._position: float = 0.0
        self._retention = retention_seconds
        self._terminal_at: Dict[str, float] = {}

    # -- order lifecycle ----------------------------------------------------

    def on_submit(self, order_id: str, side: str, price: float, size: float) -> None:
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
        self._orders[order_id] = TrackedOrder(order_id, side, price, size)

    def on_ack(self, order_id: str) -> None:
        order = self._orders.get(order_id)
        if order is not None and order.state is OrderState.SUBMITTED:
            order.state = OrderState.OPEN

    def on_cancel_sent(self, order_id: str, now: Optional[float] = None) -> None:
        order = self._orders.get(order_id)
        if order is not None and order.state in (OrderState.SUBMITTED, OrderState.OPEN):
            order.state = OrderState.CANCEL_SENT
            order.cancel_sent_at = now if now is not None else time.time()

    def on_cancel_ack(self, order_id: str, now: Optional[float] = None) -> None:
        order = self._orders.get(order_id)
        if order is not None and order.state is not OrderState.FILLED:
            order.state = OrderState.CANCELLED
            order.cancelled_at = now if now is not None else time.time()
            self._mark_terminal(order_id, now)

    # -- fills ---------------------------------------------------------------

    def on_fill(
        self,
        fill_id: str,
        order_id: str,
        side: str,
        price: float,
        size: float,
        now: Optional[float] = None,
    ) -> ClassifiedFill:
        """Classify a venue fill report and apply it to internal position.

        Position is updated for *every* non-duplicate fill, including unknown
        ones: the venue charged us for it whether we recognise it or not, and
        an honest internal position beats a tidy one.
        """
        if fill_id in self._seen_fill_ids:
            return ClassifiedFill(fill_id, order_id, side, price, size, FillClass.DUPLICATE)
        self._seen_fill_ids.add(fill_id)

        order = self._orders.get(order_id)
        if order is None:
            classification = FillClass.UNKNOWN
        elif order.state in (OrderState.CANCEL_SENT, OrderState.CANCELLED):
            classification = FillClass.POST_CANCEL
        else:
            classification = FillClass.TRACKED

        signed = size if side == "buy" else -size
        self._position += signed

        if order is not None:
            order.filled += size
            if order.filled >= order.size - 1e-12:
                order.state = OrderState.FILLED
                self._mark_terminal(order_id, now)

        return ClassifiedFill(fill_id, order_id, side, price, size, classification)

    # -- reconciliation -------------------------------------------------------

    def reconcile(
        self,
        venue_position: float,
        recent_fills: Optional[List[ClassifiedFill]] = None,
    ) -> ReconciliationReport:
        """Compare internal position with the venue's and report anomalies."""
        fills = recent_fills or []
        return ReconciliationReport(
            internal_position=self._position,
            venue_position=venue_position,
            post_cancel_fills=[f for f in fills if f.classification is FillClass.POST_CANCEL],
            unknown_fills=[f for f in fills if f.classification is FillClass.UNKNOWN],
        )

    @property
    def position(self) -> float:
        return self._position

    def open_orders(self) -> List[TrackedOrder]:
        return [
            o for o in self._orders.values()
            if o.state in (OrderState.SUBMITTED, OrderState.OPEN, OrderState.CANCEL_SENT)
        ]

    # -- internals -------------------------------------------------------------

    def _mark_terminal(self, order_id: str, now: Optional[float]) -> None:
        ts = now if now is not None else time.time()
        self._terminal_at[order_id] = ts
        self._evict(ts)

    def _evict(self, now: float) -> None:
        expired = [oid for oid, ts in self._terminal_at.items() if now - ts > self._retention]
        for oid in expired:
            self._terminal_at.pop(oid, None)
            self._orders.pop(oid, None)
