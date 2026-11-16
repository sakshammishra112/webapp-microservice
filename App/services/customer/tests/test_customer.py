from fastapi.testclient import TestClient

from services.customer.app.main import INITIAL_BALANCE, create_app


def test_customer_registration_login_and_balance_charge(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'customer.db'}")
    client = TestClient(app)
    payload = {
        "name": "Alex Johnson",
        "username": "alex.johnson",
        "email": "Alex@example.com",
        "phone": "+1-555-0100",
        "password": "correct-horse-battery",
    }

    registered = client.post("/auth/register", json=payload)
    assert registered.status_code == 201
    customer = registered.json()
    assert customer["balance"] == INITIAL_BALANCE
    assert "password" not in customer
    assert "password_hash" not in customer

    logged_in = client.post(
        "/auth/login",
        json={"username": "alex.johnson", "password": "correct-horse-battery"},
    )
    assert logged_in.status_code == 200
    assert logged_in.json()["id"] == customer["id"]

    charged = client.post(
        f"/customers/{customer['id']}/charge",
        json={"amount": 125.50},
    )
    assert charged.status_code == 200
    assert charged.json()["balance"] == 374.50


def test_customer_registration_rejects_duplicate_username(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'customer.db'}")
    client = TestClient(app)
    payload = {
        "name": "Alex Johnson",
        "username": "alex",
        "email": "alex@example.com",
        "password": "correct-horse-battery",
    }

    assert client.post("/auth/register", json=payload).status_code == 201
    duplicate = client.post(
        "/auth/register",
        json={**payload, "email": "different@example.com"},
    )
    assert duplicate.status_code == 409


def test_customer_registration_accepts_email_style_username(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'customer.db'}")
    client = TestClient(app)

    response = client.post(
        "/auth/register",
        json={
            "name": "Saksham Mishra",
            "username": "saksham_mishra@epam.com",
            "email": "saksham.mishra@example.com",
            "password": "correct-horse-battery",
        },
    )

    assert response.status_code == 201
    assert response.json()["username"] == "saksham_mishra@epam.com"
