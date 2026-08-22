from __future__ import annotations

import time
from types import SimpleNamespace

from .conftest import csrf
from .test_hosts_history import host_payload


def _wait_for(items, count=1):
    for _ in range(30):
        if len(items) >= count:
            return
        time.sleep(0.01)


def test_host_notification_preferences_default_to_global_and_are_api_visible(client, app, admin):
    host = app.extensions["hosts"].create(host_payload(), fingerprint="SHA256:notify-a", machine_id="notify-a")
    app.extensions["monitor_config"].update({"toast_events": ["host_offline"], "apprise_events": ["gpu_xid_error"]})
    response = client.get("/api/notifications/hosts")
    assert response.status_code == 200
    item = next(row for row in response.get_json()["items"] if row["host_id"] == host["id"])
    assert item["enabled"] is True and item["toast_events"] == ["host_offline"]
    assert item["apprise_events"] == ["gpu_xid_error"] and item["customized"] is False


def test_host_notification_preferences_filter_web_and_apprise_per_host(client, app, admin, monkeypatch):
    host = app.extensions["hosts"].create(host_payload(), fingerprint="SHA256:notify-b", machine_id="notify-b")
    app.extensions["monitor_config"].update({"toast_events": ["host_offline", "gpu_xid_error"], "apprise_enabled": True, "apprise_urls": ["ntfy://test-topic"], "apprise_events": ["host_offline", "gpu_xid_error"]})
    response = client.put(
        f"/api/hosts/{host['id']}/notification-settings",
        json={"enabled": True, "toast_enabled": True, "apprise_enabled": True, "toast_events": ["host_offline"], "apprise_events": ["gpu_xid_error"]},
        headers=csrf(admin),
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["settings"]["customized"] is True
    sent = []
    notifications = app.extensions["notifications"]
    monkeypatch.setattr(notifications, "_send", lambda alert, urls: sent.append(alert["alert_type"]))
    offline = app.extensions["alerts"].emit("host-offline-host-filter", host["id"], "host_offline", "critical", "offline")
    xid = app.extensions["alerts"].emit("xid-host-filter", host["id"], "gpu_xid_error", "critical", "xid")
    _wait_for(sent, 1)
    assert sent == ["gpu_xid_error"]
    listing = client.get("/api/alerts?host_id=%s" % host["id"])
    items = {item["id"]: item for item in listing.get_json()["items"]}
    assert items[offline]["notification_allowed"] is True
    assert items[xid]["notification_allowed"] is False


def test_host_notification_disable_suppresses_both_channels_and_requires_csrf(client, app, admin, monkeypatch):
    host = app.extensions["hosts"].create(host_payload(), fingerprint="SHA256:notify-c", machine_id="notify-c")
    denied = client.put(f"/api/hosts/{host['id']}/notification-settings", json={"enabled": False})
    assert denied.status_code == 403
    response = client.put(
        f"/api/hosts/{host['id']}/notification-settings",
        json={"enabled": False, "toast_enabled": True, "apprise_enabled": True, "toast_events": ["host_offline"], "apprise_events": ["host_offline"]},
        headers=csrf(admin),
    )
    assert response.status_code == 200
    notifications = app.extensions["notifications"]
    sent = []
    monkeypatch.setattr(notifications, "_send", lambda alert, urls: sent.append(alert["id"]))
    alert_id = app.extensions["alerts"].emit("disabled-host-filter", host["id"], "host_offline", "critical", "disabled")
    time.sleep(0.05)
    assert sent == []
    item = client.get(f"/api/alerts?host_id={host['id']}").get_json()["items"][0]
    assert item["id"] == alert_id and item["notification_allowed"] is False


def test_apprise_channel_remains_independent_when_web_toast_is_disabled(client, app, admin, monkeypatch):
    host = app.extensions["hosts"].create(host_payload(), fingerprint="SHA256:notify-independent", machine_id="notify-independent")
    app.extensions["monitor_config"].update({
        "toast_enabled": False,
        "apprise_enabled": True,
        "apprise_urls": ["ntfy://test-topic"],
        "apprise_events": ["host_offline"],
    })
    response = client.put(
        f"/api/hosts/{host['id']}/notification-settings",
        json={"enabled": True, "toast_enabled": True, "apprise_enabled": True, "toast_events": [], "apprise_events": ["host_offline"]},
        headers=csrf(admin),
    )
    assert response.status_code == 200
    sent = []
    notifications = app.extensions["notifications"]
    monkeypatch.setattr(notifications, "_send", lambda alert, urls: sent.append((alert["alert_type"], urls)))
    app.extensions["alerts"].emit("independent-channel", host["id"], "host_offline", "critical", "offline")
    _wait_for(sent, 1)
    assert sent == [("host_offline", ["ntfy://test-topic"])]


def test_host_notification_invalid_event_is_rejected(client, app, admin):
    host = app.extensions["hosts"].create(host_payload(), fingerprint="SHA256:notify-d", machine_id="notify-d")
    response = client.put(
        f"/api/hosts/{host['id']}/notification-settings",
        json={"enabled": True, "toast_enabled": True, "apprise_enabled": True, "toast_events": ["not-an-event"], "apprise_events": []},
        headers=csrf(admin),
    )
    assert response.status_code == 400
