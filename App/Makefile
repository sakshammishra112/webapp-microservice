.PHONY: install test run-catalog run-customer run-order run-frontend

install:
	python3 -m pip install -r requirements-dev.txt

test:
	python3 -m pytest -q

run-catalog:
	DATABASE_URL=sqlite:///./catalog.db uvicorn services.catalog.app.main:app --host 0.0.0.0 --port 8001

run-customer:
	DATABASE_URL=sqlite:///./customer.db uvicorn services.customer.app.main:app --host 0.0.0.0 --port 8002

run-order:
	DATABASE_URL=sqlite:///./order.db CATALOG_SERVICE_URL=http://localhost:8001 CUSTOMER_SERVICE_URL=http://localhost:8002 uvicorn services.order.app.main:app --host 0.0.0.0 --port 8003

run-frontend:
	CATALOG_SERVICE_URL=http://localhost:8001 CUSTOMER_SERVICE_URL=http://localhost:8002 ORDER_SERVICE_URL=http://localhost:8003 uvicorn frontend.app.main:app --host 0.0.0.0 --port 8000
