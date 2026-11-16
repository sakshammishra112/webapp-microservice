from unittest.mock import patch

from fastapi.testclient import TestClient

from frontend.app.main import app


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
        if "/customers/1/orders" in url:
            return FakeResponse(
                [
                    {
                        "id": 7,
                        "customer_id": 1,
                        "status": "CONFIRMED",
                        "total_amount": 79.99,
                        "balance_after": 420.01,
                        "items": [],
                    }
                ]
            )
        if "/customers/1" in url:
            return FakeResponse(
                {
                    "id": 1,
                    "name": "Alex Johnson",
                    "username": "alex",
                    "email": "alex@example.com",
                    "balance": 420.01,
                }
            )
        return FakeResponse([])

    async def post(self, url, json):
        if "/auth/login" in url:
            return FakeResponse(
                {
                    "id": 1,
                    "name": "Alex Johnson",
                    "username": json["username"],
                    "email": "alex@example.com",
                    "balance": 500.00,
                }
            )
        if "/orders" in url:
            return FakeResponse(
                {
                    "id": 7,
                    "customer_id": 1,
                    "status": "CONFIRMED",
                    "total_amount": 79.99,
                    "balance_after": 420.01,
                    "items": [],
                },
                status_code=201,
            )
        raise AssertionError(f"Unexpected POST URL: {url}")


def test_checkout_requires_login_and_orders_are_scoped_to_logged_in_user():
    client = TestClient(app)
    with patch("frontend.app.main.httpx.AsyncClient", FakeAsyncClient):
        unauthenticated = client.get("/checkout", follow_redirects=False)
        assert unauthenticated.status_code == 303
        assert unauthenticated.headers["location"] == "/login?next=/checkout"

        login = client.post(
            "/login",
            data={"username": "alex", "password": "correct-horse-battery", "next_path": "/checkout"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        assert login.headers["location"] == "/checkout"

        orders = client.get("/account/orders")
        assert orders.status_code == 200
        assert "Order #7" in orders.text

        checkout = client.post(
            "/checkout",
            data={"items": '[{"product_id": 1, "quantity": 1}]'},
        )
        assert checkout.status_code == 200
        assert "Remaining balance" in checkout.text
