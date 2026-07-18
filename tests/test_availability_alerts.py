"""Availability alert API helpers."""

from app.services import availability_alerts as aa


def test_module_exports():
    assert callable(aa.create_alert)
    assert callable(aa.delete_alert)
    assert callable(aa.notify_fulfilled_alerts)
    assert callable(aa.get_alert)
