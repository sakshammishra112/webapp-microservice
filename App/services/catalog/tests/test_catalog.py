from fastapi.testclient import TestClient

from services.catalog.app.main import create_app


def test_catalog_is_seeded_and_products_can_be_reserved(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'catalog.db'}")
    client = TestClient(app)

    products_response = client.get("/products")
    assert products_response.status_code == 200
    products = products_response.json()
    assert len(products) >= 4

    product = products[0]
    original_stock = product["stock_quantity"]
    reserve_response = client.post(
        "/inventory/reserve",
        json={"items": [{"product_id": product["id"], "quantity": 2}]},
    )

    assert reserve_response.status_code == 200
    updated_product = client.get(f"/products/{product['id']}").json()
    assert updated_product["stock_quantity"] == original_stock - 2


def test_catalog_rejects_insufficient_inventory(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'catalog.db'}")
    client = TestClient(app)

    response = client.post(
        "/inventory/reserve",
        json={"items": [{"product_id": 1, "quantity": 10_000}]},
    )

    assert response.status_code == 409
