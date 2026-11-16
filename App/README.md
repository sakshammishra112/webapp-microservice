# Python E-Commerce Microservices Application

## Project Overview

This project is a deliberately simple Python e-commerce application designed to demonstrate cloud infrastructure and deployment practices rather than complex business functionality.

The application will be deployed to **Azure Kubernetes Service (AKS)** using:

- Docker containers
- Azure Container Registry (ACR)
- Helm
- ArgoCD and GitOps
- Terraform-managed Azure infrastructure
- Azure Key Vault for secrets
- Prometheus, Grafana, and OpenTelemetry for observability
- Azure DevOps multi-stage CI/CD pipelines

The application consists of:

- One frontend application
- Three backend microservices
- One Azure SQL Database per environment, with service-owned tables

> **Architecture assumption:** The three microservices are backend services. The frontend is a separate deployable application in addition to those services.

## Goals

The application should provide enough functionality to demonstrate:

- Independent service deployment
- Internal service-to-service communication
- Container image creation and versioning
- Helm-based AKS deployment
- ArgoCD-based GitOps delivery
- Environment promotion from Dev to QA, Stage, and Prod
- Secure secret management with Azure Key Vault
- Application and infrastructure monitoring
- Distributed logging, metrics, and tracing

The application is intentionally not intended to be a production-grade online store. Payment processing, complex authentication, shipping integration, and advanced product functionality are outside the initial scope.

## High-Level Architecture

```text
User Browser
     |
     v
Azure Application Gateway / AKS Ingress
     |
     v
Python Frontend Service
     |
     +--> Catalog Service
     +--> Customer Service
     +--> Order Service
                    |
                    +--> Catalog Service
                    +--> Customer Service

All services own their own tables within the environment's Azure SQL Database.
```

The frontend will communicate with the backend services through internal Kubernetes DNS names. The backend services will be exposed as internal `ClusterIP` services wherever possible. Only the frontend and explicitly selected API routes will be exposed through the AKS Ingress.

### Are the internal services independent microservices?

Yes. Network visibility and service independence are separate concerns. The Catalog, Customer, and Order components are independently deployable containers with separate FastAPI applications, APIs, tests, runtime configuration, and service-owned data access. Keeping them on an internal Docker or Kubernetes network only means they are not directly reachable from the public internet.

The current design uses one Azure SQL logical server (one database per environment) for infrastructure simplicity, but the services own separate tables and access each other through HTTP APIs rather than reading each other's tables. This is an independently deployable microservice design. Separate databases or schemas can be introduced later without changing the public frontend flow.

## Recommended Technology Stack

| Component | Technology |
|---|---|
| Frontend | Python FastAPI with Jinja2 templates |
| Backend services | Python FastAPI |
| Database | Azure SQL Database (SQL Server 2022 container locally) |
| ORM | SQLAlchemy |
| Database migrations | Alembic |
| Testing | Pytest |
| Containerization | Docker multi-stage builds |
| Container registry | Azure Container Registry |
| Orchestration | Azure Kubernetes Service |
| Packaging | Helm |
| Continuous delivery | ArgoCD |
| Metrics | Prometheus-compatible metrics |
| Tracing | OpenTelemetry |
| Dashboards | Grafana |
| Secrets | Azure Key Vault with CSI Driver and Workload Identity |

Azure SQL Database is used for both the AKS and PaaS architectures, so the two deployments share the same data platform. The services connect through SQLAlchemy with the `mssql+pyodbc` dialect and Microsoft ODBC Driver 18, which is installed in each backend image. The database is not run inside the cluster; pods reach it through a private endpoint in the AKS VNet.

## Microservices

### 1. Catalog Service

**Responsibility:** Products, categories, pricing, and inventory.

#### Main features

- List products
- View product details
- Filter products by category
- Display product stock
- Reserve or release stock during order processing
- Seed sample products for demonstrations

#### Example endpoints

```text
GET  /health/live
GET  /health/ready
GET  /metrics

GET  /products
GET  /products/{product_id}
GET  /categories

POST /inventory/reserve
POST /inventory/release
```

#### Example product

```json
{
  "id": 1,
  "name": "Wireless Headphones",
  "description": "Bluetooth over-ear headphones",
  "price": 79.99,
  "category": "Electronics",
  "stock_quantity": 25,
  "image_url": "/static/images/headphones.png"
}
```

The Catalog Service owns the following tables:

- `products`
- `categories`
- `inventory`

### 2. Customer Service

**Responsibility:** Customer profiles and basic customer information.

#### Main features

- Create a customer
- Retrieve customer details
- Update customer information
- Support guest checkout using name and email
- Provide customer information for order processing

The initial version will not implement full authentication or password management. This keeps the application small and allows the project to focus on infrastructure, deployment, security, and observability. Authentication can be added later using Microsoft Entra ID or another identity provider.

#### Example endpoints

```text
GET  /health/live
GET  /health/ready
GET  /metrics

POST /customers
GET  /customers/{customer_id}
PUT  /customers/{customer_id}
GET  /customers/by-email/{email}
```

#### Example customer

```json
{
  "id": 101,
  "name": "Alex Johnson",
  "email": "alex@example.com",
  "phone": "+1-555-0100"
}
```

The Customer Service owns the `customers` table.

### 3. Order Service

**Responsibility:** Shopping carts, checkout, and order history.

#### Main features

- Create a shopping cart
- Add and remove products
- Calculate order totals
- Validate customer details
- Validate and reserve inventory
- Create orders
- View order status
- View previous orders

#### Example endpoints

```text
GET  /health/live
GET  /health/ready
GET  /metrics

POST /carts
GET  /carts/{cart_id}
POST /carts/{cart_id}/items
DELETE /carts/{cart_id}/items/{product_id}

POST /orders
GET  /orders/{order_id}
GET  /customers/{customer_id}/orders
```

#### Example order

```json
{
  "id": 5001,
  "customer_id": 101,
  "status": "CONFIRMED",
  "total_amount": 159.98,
  "items": [
    {
      "product_id": 1,
      "quantity": 2,
      "unit_price": 79.99
    }
  ]
}
```

The Order Service owns the following tables:

- `carts`
- `cart_items`
- `orders`
- `order_items`

The Order Service will call the Catalog Service to validate and reserve inventory. It will call the Customer Service to validate the customer. It will not directly access tables owned by those services.

For simplicity, payment processing will be mocked. The initial order status flow will be:

```text
PENDING -> CONFIRMED -> SHIPPED
```

## Frontend Application

The frontend will be a small server-rendered Python application using FastAPI and Jinja2 templates.

### Planned pages

```text
/                       Home page
/products               Product catalog
/products/{id}          Product details
/cart                   Shopping cart
/checkout               Checkout form
/orders/{id}            Order confirmation
/customer/orders        Customer order history
```

The frontend will call backend services through internal Kubernetes DNS names such as:

```text
http://catalog-service
http://customer-service
http://order-service
```

This keeps the backend services private and avoids unnecessary CORS configuration.

## Database Design

The application uses one Azure SQL logical server with a database per environment (`sqldb-cloudcart-dev`, `-qa`, `-stage`, `-prod`). Within each database the services keep logical separation by table ownership.

```text
Azure SQL Database (per environment)
|
+-- catalog schema
|   +-- products
|   +-- categories
|   +-- inventory
|
+-- customer schema
|   +-- customers
|
+-- order schema
    +-- carts
    +-- cart_items
    +-- orders
    +-- order_items
```

This keeps the infrastructure simple while maintaining service ownership boundaries.

Each service will have:

- Its own SQLAlchemy models
- Its own Alembic migration directory
- Its own database schema
- Restricted database permissions where practical

Database credentials will not be stored in Git. Terraform will provision the required secret references in Azure Key Vault. Kubernetes will retrieve secrets using the Azure Key Vault CSI Driver and managed identity or workload identity.

## Recommended Repository Structure

The project follows the multi-repository structure described in the overall cloud infrastructure project.

### Application repository

```text
ecommerce-app/
├── frontend/
│   ├── app/
│   ├── templates/
│   ├── static/
│   ├── tests/
│   ├── Dockerfile
│   └── requirements.txt
│
├── services/
│   ├── catalog/
│   │   ├── app/
│   │   ├── migrations/
│   │   ├── tests/
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   │
│   ├── customer/
│   │   ├── app/
│   │   ├── migrations/
│   │   ├── tests/
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   │
│   └── order/
│       ├── app/
│       ├── migrations/
│       ├── tests/
│       ├── Dockerfile
│       └── requirements.txt
│
├── docker-compose.yml
├── Makefile
└── README.md
```

The application repository will contain application source code, tests, Dockerfiles, and local development configuration.

### Infrastructure repository

```text
ecommerce-infra/
├── environments/
│   ├── dev/
│   ├── qa/
│   ├── stage/
│   └── prod/
│
├── modules/
│   ├── aks/
│   ├── acr/
│   ├── key-vault/
│   ├── sql-database/
│   ├── monitoring/
│   └── networking/
│
├── backend.tf
├── main.tf
├── variables.tf
├── locals.tf
├── outputs.tf
└── README.md
```

The infrastructure repository will contain modular Terraform code for AKS, ACR, Azure SQL, Key Vault, networking, monitoring, and remote state storage.

### Pipelines and GitOps repository

```text
ecommerce-gitops/
├── helm/
│   └── ecommerce/
│       ├── Chart.yaml
│       ├── values.yaml
│       ├── values-dev.yaml
│       ├── values-qa.yaml
│       ├── values-stage.yaml
│       ├── values-prod.yaml
│       └── templates/
│
├── argocd/
│   ├── projects/
│   ├── applications/
│   └── repositories/
│
├── pipelines/
│   ├── templates/
│   │   ├── python-ci.yml
│   │   ├── docker-build.yml
│   │   ├── security-scan.yml
│   │   └── gitops-update.yml
│   └── infra-pipeline.yml
│
└── README.md
```

The GitOps repository will contain Helm charts, ArgoCD manifests, environment-specific configuration, and reusable Azure DevOps pipeline templates.

## Kubernetes Deployment Model

Each component will run as a separate Kubernetes Deployment:

```text
frontend
catalog-service
customer-service
order-service
```

Each deployment should include:

- Kubernetes Deployment
- ClusterIP Service
- ConfigMap
- Secret reference
- Readiness probe
- Liveness probe
- Resource requests and limits
- Horizontal Pod Autoscaler where appropriate
- Prometheus scrape annotations
- Labels for Grafana dashboards

Recommended namespaces are:

```text
ecommerce-dev
ecommerce-qa
ecommerce-stage
ecommerce-prod
```

ArgoCD will deploy the same Helm chart to each namespace using environment-specific values.

## CI/CD Plan

### Application CI pipeline

The application pipeline will run on pull requests and application branches.

Planned stages:

1. Checkout source
2. Install Python dependencies
3. Run formatting and lint checks
4. Run unit tests
5. Run API contract tests
6. Build Docker images
7. Run container vulnerability scanning
8. Push images to ACR
9. Update the GitOps repository with the new image tags

Images will use the Azure DevOps Build ID for unique versioning:

```text
catalog-service:$(Build.BuildId)
customer-service:$(Build.BuildId)
order-service:$(Build.BuildId)
frontend:$(Build.BuildId)
```

### GitOps deployment flow

```text
Developer commits code
        |
        v
Azure DevOps CI pipeline
        |
        +--> Test
        +--> Build image
        +--> Scan image
        +--> Push to ACR
        +--> Update Helm image tag
                    |
                    v
              GitOps repository
                    |
                    v
                  ArgoCD
                    |
                    v
              AKS environment
```

Promotion will be handled by changing the image tag or Helm values for:

```text
Dev -> QA -> Stage -> Prod
```

Production deployment will require an Azure DevOps approval gate. The production apply and promotion stages will be restricted to the main branch.

## Observability Plan

Every Python service will expose the following endpoints:

```text
/health/live
/health/ready
/metrics
```

The services will produce structured JSON logs containing:

- Service name
- Environment
- Request ID
- Trace ID
- HTTP method
- Route
- HTTP status code
- Request duration
- Error details

OpenTelemetry instrumentation will be added for:

- HTTP requests
- Database queries
- Frontend-to-service calls
- Order-to-Catalog Service calls
- Order-to-Customer Service calls

### Cluster dashboard

Grafana dashboards will track:

- Node CPU and memory
- Pod CPU and memory
- Pod restarts
- Deployment availability
- Namespace resource consumption

### Application dashboard

Application dashboards will track:

- Requests per second
- HTTP 4xx and 5xx rates
- P95 and P99 response latency
- Orders created
- Failed orders
- Inventory reservation failures
- Database connection errors

### Service dependency dashboard

Dependency dashboards will track:

- Frontend-to-Catalog Service latency
- Frontend-to-Order Service latency
- Order-to-Catalog Service failures
- Order-to-Customer Service failures

## Implementation Phases

### Phase 1: Repository and application skeleton

- Create frontend and service folders
- Add FastAPI startup code
- Add health endpoints
- Add local configuration
- Add Docker Compose

### Phase 2: Implement business functions

- Add Catalog Service APIs
- Add Customer Service APIs
- Add cart and order APIs
- Add SQLAlchemy models
- Add Alembic migrations
- Add sample data

### Phase 3: Build the basic website

- Product listing page
- Product detail page
- Cart page
- Checkout page
- Order confirmation page
- Customer order history page

### Phase 4: Add automated tests

- Unit tests for each service
- API tests
- Database repository tests
- Order and inventory integration tests
- Frontend smoke tests

### Phase 5: Containerization

- Add optimized multi-stage Dockerfiles
- Run containers as non-root users
- Add health checks
- Add image metadata and Build ID tags
- Scan images for vulnerabilities

### Phase 6: Helm and AKS deployment

- Create the Helm chart
- Add Deployments and Services
- Add Ingress
- Add resource limits
- Add probes and autoscaling
- Add environment-specific Helm values

### Phase 7: Security integration

- Store database credentials in Azure Key Vault
- Configure AKS Workload Identity
- Configure the Key Vault CSI Driver
- Remove secrets from configuration files
- Add Kubernetes NetworkPolicies where appropriate

### Phase 8: GitOps and observability

- Configure ArgoCD Applications
- Deploy Dev, QA, Stage, and Prod namespaces
- Add Prometheus metrics
- Add OpenTelemetry tracing
- Deploy Grafana dashboards
- Configure alerts

## Initial Scope Boundaries

To keep the application focused on cloud infrastructure and deployment, the first version will exclude:

- Real payment gateway integration
- Complex authentication and authorization
- Product administration portal
- Shipping carrier integration
- Product reviews
- Coupons and promotions
- Recommendation engine
- Message broker
- Event sourcing

These capabilities can be added later, but they would increase application complexity without significantly improving the initial AKS and CI/CD demonstration.

## Expected Result

The finished application should demonstrate:

- Four independently deployable Python containers
- Three backend microservices with clear ownership
- Service-to-service communication
- Database schema separation
- Docker image versioning
- ACR integration
- Helm-based deployment
- ArgoCD GitOps delivery
- Environment promotion
- Key Vault-based secret management
- Kubernetes health checks and autoscaling
- Prometheus metrics, Grafana dashboards, logs, and traces

This provides a realistic but manageable e-commerce workload while keeping the main project emphasis on **Azure infrastructure, AKS deployment, GitOps, security, and observability**.

## Current Implementation Status

The first runnable application slice is now included in this repository:

- FastAPI Catalog Service with seeded products and inventory reservation
- FastAPI Customer Service with idempotent customer creation by email
- FastAPI Customer Service with username/password registration and login
- Passwords stored as salted PBKDF2 hashes rather than plaintext credentials
- A fixed `$500.00` starting balance for every newly registered customer
- FastAPI Order Service with dependency validation, inventory reservation, balance deduction, and order persistence
- FastAPI/Jinja2 frontend with product browsing, cart, checkout, and order confirmation
- Login, registration, logout, account balance, and user-specific order history pages
- Checkout moved to a separate authenticated page; the cart no longer contains the checkout form
- SQLAlchemy persistence with SQLite support for unit tests and Azure SQL / SQL Server (`mssql+pyodbc`) for Compose and AKS
- Multi-stage Dockerfiles for all four deployable components
- Local Docker Compose configuration with SQL Server 2022 as a stand-in for Azure SQL
- Initial Helm chart for the four AKS Deployments, Services, probes, autoscaling, and Ingress
- Initial ArgoCD Application manifest for the Dev namespace
- Health, readiness, and Prometheus-compatible metrics endpoints
- Initial tests for catalog, customer, and order behavior

### Local setup

Install the development dependencies:

```bash
python3 -m pip install -r requirements-dev.txt
```

Run the automated tests:

```bash
python3 -m pytest -q
```

To run the application without Docker, start the services in separate terminals:

```bash
make run-catalog
make run-customer
make run-order
make run-frontend
```

Then open `http://localhost:8000` in a browser.

When Docker is available, the full local stack can be started with:

```bash
docker compose up --build
```

The Compose stack runs SQL Server 2022 (`mcr.microsoft.com/mssql/server`) and a one-shot `mssql-init` container that creates the `ecommerce` database, mirroring the database Terraform creates in Azure. SQL Server needs about 2 GB of memory for Docker.

### Database connection

Every backend service reads a single `DATABASE_URL` environment variable containing a SQLAlchemy URL. In Azure this value is generated by Terraform and stored in Key Vault as `<env>-database-url`; it is never committed:

```text
mssql+pyodbc://<user>:<password>@<server>.database.windows.net:1433/<database>?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no&Connection+Timeout=30
```

Unit tests and the `make run-*` targets use SQLite, so no ODBC driver is needed on a developer machine.

The local Compose stack exposes the frontend on port `8000`. The Catalog, Customer, and Order Services are intentionally private to the Compose network because the frontend and Order Service access them through service DNS names. This also avoids host-port conflicts.

For an existing local database volume, the Customer and Order services include a small compatibility migration for the new authentication and balance columns. For a clean development reset, use:

```bash
docker compose down -v
docker compose up --build
```

Do not use `down -v` in an environment where you need to preserve database data.

## CI/CD pipeline

This repo holds no pipeline YAML. The pipeline is defined entirely in the
`Pipelines` repo, at `app-pipeline/cicd-pipeline.yml`, which checks out this
repo as the `app` resource (same pattern as `infra-pipeline/` there for the
`Infra` repo):

- **Every branch and PR**: ruff + compile, unit tests, `pip-audit`, a Docker
  Compose smoke test against SQL Server, and an image build with a Trivy scan
  (HIGH/CRITICAL, fixable OS vulnerabilities fail the build).
- **main**: additionally pushes `acrcloudcartdsffj.azurecr.io/<service>:<BuildId>`
  and deploys through GitOps: each stage (dev → qa → stage → prod) commits the
  tag to `gitops/helm/<service>/values-<env>.yaml` after the approval on the
  `app-<env>` Environment, and Argo CD syncs it.

On AKS the services read secrets from files mounted from Key Vault:
`DATABASE_URL_FILE` and `SESSION_SECRET_FILE` take precedence over
`DATABASE_URL` / `SESSION_SECRET`.
