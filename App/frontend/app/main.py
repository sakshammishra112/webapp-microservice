import json
import os

import httpx
from fastapi import FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware


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


CATALOG_SERVICE_URL = os.getenv("CATALOG_SERVICE_URL", "http://localhost:8001").rstrip("/")
CUSTOMER_SERVICE_URL = os.getenv("CUSTOMER_SERVICE_URL", "http://localhost:8002").rstrip("/")
ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://localhost:8003").rstrip("/")
SESSION_SECRET = _setting("SESSION_SECRET", "cloudcart-local-development-secret")

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))
app = FastAPI(title="E-Commerce Frontend", version="0.1.0")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=60 * 60 * 8)
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "..", "static")),
    name="static",
)


def _render(request: Request, template: str, **context):
    context.setdefault("user", request.session.get("user"))
    return templates.TemplateResponse(request=request, name=template, context=context)


def _render_status(request: Request, template: str, status_code: int, **context):
    response = _render(request, template, **context)
    response.status_code = status_code
    return response


def _safe_next(value: str | None) -> str:
    return value if value and value.startswith("/") and not value.startswith("//") else "/products"


def _require_login(request: Request, next_path: str = "/checkout"):
    if request.session.get("user") is None:
        return RedirectResponse(
            url=f"/login?next={next_path}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return None


async def _get_json(url: str):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"Backend service unavailable: {exc}") from exc


@app.get("/health/live")
async def liveness():
    return {"status": "ok", "service": "frontend"}


@app.get("/health/ready")
async def readiness():
    return {"status": "ready", "service": "frontend"}


@app.get("/", response_class=HTMLResponse)
async def home():
    return RedirectResponse(url="/products", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@app.get("/products", response_class=HTMLResponse)
async def products(request: Request):
    product_list = await _get_json(f"{CATALOG_SERVICE_URL}/products")
    return _render(request, "products.html", products=product_list, title="Products")


@app.get("/products/{product_id}", response_class=HTMLResponse)
async def product_details(request: Request, product_id: int):
    product = await _get_json(f"{CATALOG_SERVICE_URL}/products/{product_id}")
    return _render(request, "product.html", product=product, title=product["name"])


@app.get("/cart", response_class=HTMLResponse)
async def cart(request: Request):
    return _render(request, "cart.html", title="Your Cart")


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return _render(request, "register.html", title="Create account", next=_safe_next(request.query_params.get("next")))


@app.post("/register", response_class=HTMLResponse)
async def register(
    request: Request,
    name: str = Form(...),
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    phone: str = Form(default=""),
    next_path: str = Form(default="/products"),
):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{CUSTOMER_SERVICE_URL}/auth/register",
                json={
                    "name": name,
                    "username": username,
                    "email": email,
                    "password": password,
                    "phone": phone or None,
                },
            )
            response.raise_for_status()
            customer = response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.json().get("detail", "Could not create account")
        return _render_status(
            request, "error.html", exc.response.status_code, title="Registration Error", message=detail
        )
    except httpx.HTTPError as exc:
        return _render_status(request, "error.html", 503, title="Registration Error", message=str(exc))

    request.session["user"] = {
        "id": customer["id"],
        "username": customer["username"],
        "name": customer["name"],
        "balance": customer["balance"],
    }
    return RedirectResponse(url=_safe_next(next_path), status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return _render(request, "login.html", title="Login", next=_safe_next(request.query_params.get("next")))


@app.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_path: str = Form(default="/products"),
):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{CUSTOMER_SERVICE_URL}/auth/login",
                json={"username": username, "password": password},
            )
            response.raise_for_status()
            customer = response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.json().get("detail", "Invalid username or password")
        return _render_status(
            request,
            "login.html",
            exc.response.status_code,
            title="Login",
            next=_safe_next(next_path),
            error=detail,
        )
    except httpx.HTTPError as exc:
        return _render_status(request, "error.html", 503, title="Login Error", message=str(exc))

    request.session["user"] = {
        "id": customer["id"],
        "username": customer["username"],
        "name": customer["name"],
        "balance": customer["balance"],
    }
    return RedirectResponse(url=_safe_next(next_path), status_code=status.HTTP_303_SEE_OTHER)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/products", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/checkout", response_class=HTMLResponse)
async def checkout_page(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    customer = await _get_json(f"{CUSTOMER_SERVICE_URL}/customers/{request.session['user']['id']}")
    return _render(request, "checkout.html", customer=customer, title="Checkout")


@app.post("/checkout", response_class=HTMLResponse)
async def checkout(request: Request, items: str = Form(...)):
    redirect = _require_login(request)
    if redirect:
        return redirect

    try:
        cart_items = json.loads(items)
        if not isinstance(cart_items, list) or not cart_items:
            raise ValueError("Cart is empty")
    except (TypeError, json.JSONDecodeError, ValueError) as exc:
        return _render_status(request, "error.html", 400, title="Checkout Error", message=str(exc))

    customer_id = request.session["user"]["id"]
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            order_response = await client.post(
                f"{ORDER_SERVICE_URL}/orders",
                json={
                    "customer_id": customer_id,
                    "items": [
                        {"product_id": item["product_id"], "quantity": item["quantity"]}
                        for item in cart_items
                    ],
                },
            )
            order_response.raise_for_status()
            order = order_response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.json().get("detail", "Checkout failed")
        return _render_status(request, "error.html", exc.response.status_code, title="Checkout Error", message=detail)
    except httpx.HTTPError as exc:
        return _render_status(request, "error.html", 503, title="Checkout Error", message=str(exc))

    request.session["user"]["balance"] = order["balance_after"]
    return _render(request, "order.html", order=order, title="Order Confirmed")


@app.get("/account/orders", response_class=HTMLResponse)
async def account_orders(request: Request):
    redirect = _require_login(request, "/account/orders")
    if redirect:
        return redirect
    customer_id = request.session["user"]["id"]
    orders = await _get_json(f"{ORDER_SERVICE_URL}/customers/{customer_id}/orders")
    return _render(request, "orders.html", orders=orders, title="My Orders")


@app.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_details(request: Request, order_id: int):
    redirect = _require_login(request, f"/orders/{order_id}")
    if redirect:
        return redirect
    order = await _get_json(f"{ORDER_SERVICE_URL}/orders/{order_id}")
    if order["customer_id"] != request.session["user"]["id"]:
        raise HTTPException(status_code=403, detail="You cannot view another customer's order")
    return _render(request, "order.html", order=order, title=f"Order #{order_id}")
