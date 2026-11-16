import os
import time
from typing import Generator

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, CollectorRegistry, generate_latest
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Float, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    stock_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    image_url: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ProductCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(min_length=2)
    category: str = Field(min_length=2, max_length=80)
    price: float = Field(gt=0)
    stock_quantity: int = Field(ge=0)
    image_url: str | None = None


class ProductRead(ProductCreate):
    id: int
    model_config = ConfigDict(from_attributes=True)


class InventoryItem(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)


class InventoryRequest(BaseModel):
    items: list[InventoryItem] = Field(min_length=1)


SEED_PRODUCTS = [
    {
        "name": "Wireless Headphones",
        "description": "Bluetooth over-ear headphones with active noise cancellation.",
        "category": "Electronics",
        "price": 79.99,
        "stock_quantity": 25,
        "image_url": "/static/images/headphones.png",
    },
    {
        "name": "Mechanical Keyboard",
        "description": "Compact mechanical keyboard with hot-swappable switches.",
        "category": "Electronics",
        "price": 109.00,
        "stock_quantity": 18,
        "image_url": "/static/images/keyboard.png",
    },
    {
        "name": "Travel Backpack",
        "description": "Water-resistant 28-liter backpack for everyday travel.",
        "category": "Accessories",
        "price": 64.50,
        "stock_quantity": 32,
        "image_url": "/static/images/backpack.png",
    },
    {
        "name": "Insulated Water Bottle",
        "description": "Double-wall stainless steel bottle that keeps drinks cold.",
        "category": "Lifestyle",
        "price": 24.95,
        "stock_quantity": 40,
        "image_url": "/static/images/bottle.png",
    },
]


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


def create_app(database_url: str | None = None) -> FastAPI:
    database_url = database_url or _setting("DATABASE_URL", "sqlite:///./catalog.db")
    engine = _database_engine(database_url)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)

    with session_factory() as db:
        if db.scalar(select(Product.id).limit(1)) is None:
            db.add_all(Product(**product) for product in SEED_PRODUCTS)
            db.commit()

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

    app = FastAPI(title="Catalog Service", version="0.1.0")
    app.state.session_factory = session_factory
    app.state.registry = registry

    @app.middleware("http")
    async def collect_metrics(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        path = request.url.path
        request_count.labels("catalog", request.method, path, response.status_code).inc()
        request_latency.labels("catalog", request.method, path).observe(time.perf_counter() - started)
        return response

    def get_db() -> Generator[Session, None, None]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    @app.get("/health/live")
    def liveness():
        return {"status": "ok", "service": "catalog"}

    @app.get("/health/ready")
    def readiness(db: Session = Depends(get_db)):
        db.scalar(select(Product.id).limit(1))
        return {"status": "ready", "service": "catalog"}

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/products", response_model=list[ProductRead])
    def list_products(category: str | None = Query(default=None), db: Session = Depends(get_db)):
        query = select(Product).order_by(Product.id)
        if category:
            query = query.where(Product.category == category)
        return list(db.scalars(query).all())

    @app.get("/products/{product_id}", response_model=ProductRead)
    def get_product(product_id: int, db: Session = Depends(get_db)):
        product = db.get(Product, product_id)
        if product is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
        return product

    @app.get("/categories", response_model=list[str])
    def list_categories(db: Session = Depends(get_db)):
        return list(db.scalars(select(Product.category).distinct().order_by(Product.category)).all())

    @app.post("/products", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
    def create_product(payload: ProductCreate, db: Session = Depends(get_db)):
        product = Product(**payload.model_dump())
        db.add(product)
        db.commit()
        db.refresh(product)
        return product

    @app.post("/inventory/reserve")
    def reserve_inventory(payload: InventoryRequest, db: Session = Depends(get_db)):
        products = []
        for item in payload.items:
            product = db.get(Product, item.product_id)
            if product is None:
                raise HTTPException(status_code=404, detail=f"Product {item.product_id} not found")
            if product.stock_quantity < item.quantity:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Insufficient stock for product {item.product_id}",
                )
            products.append((product, item.quantity))

        for product, quantity in products:
            product.stock_quantity -= quantity
        db.commit()
        return {
            "reserved": [
                {"product_id": product.id, "quantity": quantity} for product, quantity in products
            ]
        }

    @app.post("/inventory/release")
    def release_inventory(payload: InventoryRequest, db: Session = Depends(get_db)):
        for item in payload.items:
            product = db.get(Product, item.product_id)
            if product is not None:
                product.stock_quantity += item.quantity
        db.commit()
        return {"released": payload.model_dump()["items"]}

    return app


app = create_app()
