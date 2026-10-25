from fastapi.testclient import TestClient

from alertcheck.web import app


def test_public_page_uses_same_origin_static_assets():
    client = TestClient(app)
    page = client.get("/")
    assert page.status_code == 200
    for asset in ("/static/app.css", "/static/ops.css", "/static/favicon.svg"):
        assert f'href="{asset}"' in page.text
        assert client.get(asset).status_code == 200
