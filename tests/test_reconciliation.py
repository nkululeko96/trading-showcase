import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from showcase import FillClass, FillReconciler, OrderState


def make_reconciler():
    r = FillReconciler(retention_seconds=300.0)
    r.on_submit("o1", "buy", 100.0, 2.0)
    r.on_ack("o1")
    return r


def test_normal_fill_is_tracked_and_updates_position():
    r = make_reconciler()
    fill = r.on_fill("f1", "o1", "buy", 100.0, 2.0)
    assert fill.classification is FillClass.TRACKED
    assert r.position == 2.0
    assert not r.open_orders()


def test_partial_fill_keeps_order_open():
    r = make_reconciler()
    r.on_fill("f1", "o1", "buy", 100.0, 0.5)
    assert len(r.open_orders()) == 1
    assert r.position == 0.5


def test_fill_after_cancel_sent_is_post_cancel_race():
    r = make_reconciler()
    r.on_cancel_sent("o1", now=10.0)
    fill = r.on_fill("f1", "o1", "buy", 100.0, 2.0, now=10.1)
    assert fill.classification is FillClass.POST_CANCEL
    # the fill still moves position: the venue's truth wins
    assert r.position == 2.0


def test_fill_after_cancel_ack_is_still_matched_within_retention():
    r = make_reconciler()
    r.on_cancel_sent("o1", now=10.0)
    r.on_cancel_ack("o1", now=10.2)
    fill = r.on_fill("f1", "o1", "buy", 100.0, 1.0, now=11.0)
    assert fill.classification is FillClass.POST_CANCEL


def test_unknown_fill_is_flagged_but_position_updated():
    r = make_reconciler()
    fill = r.on_fill("f1", "ghost-order", "sell", 99.0, 1.5)
    assert fill.classification is FillClass.UNKNOWN
    assert r.position == -1.5


def test_duplicate_fill_id_is_ignored_for_position():
    r = make_reconciler()
    r.on_fill("f1", "o1", "buy", 100.0, 1.0)
    dup = r.on_fill("f1", "o1", "buy", 100.0, 1.0)
    assert dup.classification is FillClass.DUPLICATE
    assert r.position == 1.0


def test_reconcile_reports_drift_and_anomalies():
    r = make_reconciler()
    f1 = r.on_fill("f1", "ghost", "buy", 100.0, 1.0)
    report = r.reconcile(venue_position=2.0, recent_fills=[f1])
    assert report.unknown_fills == [f1]
    assert report.position_drift == 1.0  # venue says 2.0, we accumulated 1.0
    assert not report.clean


def test_reconcile_clean_when_in_sync():
    r = make_reconciler()
    f1 = r.on_fill("f1", "o1", "buy", 100.0, 2.0)
    report = r.reconcile(venue_position=2.0, recent_fills=[f1])
    assert report.clean


def test_terminal_orders_evicted_after_retention():
    r = FillReconciler(retention_seconds=60.0)
    r.on_submit("o1", "buy", 100.0, 1.0)
    r.on_ack("o1")
    r.on_cancel_sent("o1", now=0.0)
    r.on_cancel_ack("o1", now=1.0)
    # new terminal event well past retention triggers eviction of o1
    r.on_submit("o2", "buy", 100.0, 1.0)
    r.on_cancel_sent("o2", now=100.0)
    r.on_cancel_ack("o2", now=100.0)
    fill = r.on_fill("f1", "o1", "buy", 100.0, 1.0, now=100.0)
    assert fill.classification is FillClass.UNKNOWN


def test_full_fill_marks_state_filled_before_cancel_ack():
    r = make_reconciler()
    r.on_fill("f1", "o1", "buy", 100.0, 2.0)
    r.on_cancel_ack("o1")  # cancel ack after full fill must not un-fill it
    assert not r.open_orders()
    report = r.reconcile(venue_position=2.0)
    assert report.clean
