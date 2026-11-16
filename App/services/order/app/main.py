import os
import time
from datetime import datetime, timezone
from typing import Generator

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, create_engine, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="CONFIRMED")
    total_amount: Mapped[float] = mapped_column(Float, nullable=False)
    balance_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    product_name: Mapped[str] = mapped_column(String(120), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False)
    order: Mapped[Order] = relationship(back_populates="items")


class OrderItemRequest(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)


class OrderCreate(BaseModel):
    customer_id: int
    items: list[OrderItemRequest] = Field(min_length=1)


class OrderItemRead(BaseModel):
    product_id: int
    product_name: str
    quantity: int
    unit_price: float
    model_config = ConfigDict(from_attributes=True)


class OrderRead(BaseModel):
    id: int
    customer_id: int
    status: str
    total_amount: float
    balance_after: float | None
    created_at: datetime
    items: list[OrderItemRead]
    model_config = ConfigDict(from_attributes=True)


def _setting(name: str, default: str) -> str:
    """Return NAME, or the contents of the file named by NAME_FILE.

    The *_FILE form is used on AKS, where Key Vault secrets are mounted as
    files by the Secrets Store CSI driver instead of being exposed as env vars.
    """
    path = os.getenv(f"{name}_FILE")
    if path:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    return os.getenv(name, default)


def _database_engine(database_url: str):
    if database_url.startswith("sqlite"):
        return create_engine(database_url, connect_args={"check_same_thread": False})
    # Azure SQL drops idle connections (and serverless databases auto-pause),
    # so validate pooled connections before use and recycle them periodically.
    return create_engine(database_url, pool_pre_ping=True, pool_recycle=1800)


def _prepare_schema(engine) -> None:
    Base.metadata.create_all(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("orders")}
    if "balance_after" not in columns:
        with engine.begin() as connection:
            # "ADD" without "COLUMN" is valid on SQLite, PostgreSQL and SQL Server.
            connection.execute(text("ALTER TABLE orders ADD balance_after FLOAT"))


def create_app(database_url: str | None = None) -> FastAPI:
    database_url = database_url or _setting("DATABASE_URL", "sqlite:///./order.db")
    engine = _database_engine(database_url)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    _prepare_schema(engine)

    catalog_url = os.getenv("CATALOG_SERVICE_URL", "http://localhost:8001").rstrip("/")
    customer_url = os.getenv("CUSTOMER_SERVICE_URL", "http://localhost:8002").rstrip("/")

    registry = CollectorRegistry()
    request_count = Counter(
        "http_requests_total",
        "Total HTTP requests",
        ["service", "method", "path", "status"],
        registry=registry,
    )
    request_latency = Histogram(
        "http_request_duration_seconds",
        "HTTP request duration in seconds",
        ["service", "method", "path"],
        registry=registry,
    )

    app = FastAPI(title="Order Service", version="0.1.0")
    app.state.session_factory = session_factory

    @app.middleware("http")
    async def collect_metrics(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        path = request.url.path
        request_count.labels("order", request.method, path, response.status_code).inc()
        request_latency.labels("order", request.method, path).observe(time.perf_counter() - started)
        return response

    def get_db() -> Generator[Session, None, None]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    @app.get("/health/live")
    def liveness():
        return {"status": "ok", "service": "order"}

    @app.get("/health/ready")
    def readiness(db: Session = Depends(get_db)):
        db.scalar(select(Order.id).limit(1))
        return {"status": "ready", "service": "order"}

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    async def _release_inventory(client: httpx.AsyncClient, items: list[OrderItemRequest]):
        await client.post(
            f"{catalog_url}/inventory/release",
            json={"items": [item.model_dump() for item in items]},
        )

    async def _refund_customer(client: httpx.AsyncClient, customer_id: int, amount: float):
        await client.post(
            f"{customer_url}/customers/{customer_id}/refund",
            json={"amount": amount},
        )

    @app.post("/orders", response_model=OrderRead, status_code=status.HTTP_201_CREATED)
    async def create_order(payload: OrderCreate, db: Session = Depends(get_db)):
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                customer_response = await client.get(f"{customer_url}/customers/{payload.customer_id}")
                if customer_response.status_code == 404:
                    raise HTTPException(status_code=400, detail="Customer not found")
                customer_response.raise_for_status()

                catalog_items = []
                for requested_item in payload.items:
                    product_response = await client.get(
                        f"{catalog_url}/products/{requested_item.product_id}"
                    )
                    if product_response.status_code == 404:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Product {requested_item.product_id} not found",
                        )
                    product_response.raise_for_status()
                    product = product_response.json()
                    catalog_items.append(
                        {
                            "product_id": requested_item.product_id,
                            "product_name": product["name"],
                            "quantity": requested_item.quantity,
                            "unit_price": product["price"],
                        }
                    )

                reserve_response = await client.post(
                    f"{catalog_url}/inventory/reserve",
                    json={"items": [item.model_dump() for item in payload.items]},
                )
                if reserve_response.status_code == 409:
                    raise HTTPException(status_code=409, detail=reserve_response.json().get("detail"))
                reserve_response.raise_for_status()
            except HTTPException:
                raise
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=503, detail=f"Dependency unavailable: {exc}") from exc

            total_amount = round(
                sum(item["quantity"] * item["unit_price"] for item in catalog_items), 2
            )
            try:
                charge_response = await client.post(
                    f"{customer_url}/customers/{payload.customer_id}/charge",
                    json={"amount": total_amount},
                )
                if charge_response.status_code == 409:
                    await _release_inventory(client, payload.items)
                    raise HTTPException(status_code=402, detail="Insufficient account balance")
                if charge_response.status_code == 404:
                    await _release_inventory(client, payload.items)
                    raise HTTPException(status_code=400, detail="Customer not found")
                charge_response.raise_for_status()
                charged_customer = charge_response.json()
            except HTTPException:
                raise
            except httpx.HTTPError as exc:
                await _release_inventory(client, payload.items)
                raise HTTPException(status_code=503, detail=f"Payment service unavailable: {exc}") from exc

            order = Order(
                customer_id=payload.customer_id,
                status="CONFIRMED",
                total_amount=total_amount,
                balance_after=round(charged_customer["balance"], 2),
            )
            order.items = [OrderItem(**item) for item in catalog_items]
            try:
                db.add(order)
                db.commit()
                db.refresh(order)
            except Exception:
                db.rollback()
                await _release_inventory(client, payload.items)
                await _refund_customer(client, payload.customer_id, total_amount)
                raise HTTPException(status_code=500, detail="Could not create order")
            return order

    @app.get("/orders/{order_id}", response_model=OrderRead)
    def get_order(order_id: int, db: Session = Depends(get_db)):
        order = db.get(Order, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found")
        return order

    @app.get("/customers/{customer_id}/orders", response_model=list[OrderRead])
    def list_customer_orders(customer_id: int, db: Session = Depends(get_db)):
        query = select(Order).where(Order.customer_id == customer_id).order_by(Order.created_at.desc())
        return list(db.scalars(query).all())

    return app


app = create_app()
