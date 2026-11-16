from unittest.mock import patch

from fastapi.testclient import TestClient

from services.order.app.main import create_app


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get(self, url):
        if "/customers/101" in url:
            return FakeResponse(
                {"id": 101, "name": "Alex", "email": "alex@example.com", "balance": 500.00}
            )
        return FakeResponse(
            {
                "id": 1,
                "name": "Wireless Headphones",
                "price": 79.99,
                "stock_quantity": 10,
            }
        )

    async def post(self, url, json):
        if "/charge" in url:
            return FakeResponse({"id": 101, "balance": 340.02})
        return FakeResponse({"reserved": json.get("items", [])})


def test_order_validates_dependencies_and_persists_order(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'order.db'}")
    client = TestClient(app)
    payload = {
        "customer_id": 101,
        "items": [{"product_id": 1, "quantity": 2}],
    }

    with patch("services.order.app.main.httpx.AsyncClient", FakeAsyncClient):
        response = client.post("/orders", json=payload)

    assert response.status_code == 201
    order = response.json()
    assert order["status"] == "CONFIRMED"
    assert order["total_amount"] == 159.98
    assert order["balance_after"] == 340.02
    assert order["items"][0]["product_name"] == "Wireless Headphones"

    stored = client.get(f"/orders/{order['id']}")
    assert stored.status_code == 200
    assert stored.json()["customer_id"] == 101
