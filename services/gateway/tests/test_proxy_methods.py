"""The /los proxy forwards only GET and POST (services/gateway/app/main.py).

PR regression: the monthly_debt remediation endpoint was first written as
PATCH /applications/{id}/monthly-debt, which is unreachable through the product
front door because the gateway does not proxy PATCH — so a legacy NULL-debt row
could never be cleared through the gateway. The endpoint is now POST; this test
locks the constraint that any LOS write must use a method the gateway proxies, so
a future PATCH endpoint on origination re-surfaces the gap here instead of shipping.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_los_proxy_rejects_patch():
    # PATCH is refused at the routing layer (405) before any downstream forward,
    # so this needs no live origination service.
    resp = client.patch("/los/applications/1/monthly-debt", json={"monthly_debt": 450})
    assert resp.status_code == 405


def test_los_route_allows_get_and_post_only():
    route = next(r for r in app.routes if getattr(r, "path", "") == "/los/{path:path}")
    assert route.methods == {"GET", "POST"}
