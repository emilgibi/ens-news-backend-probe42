
# COE-POC-BACKEND

This repository contains a FastAPI-based backend application designed for a (COE) Proof of Concept (POC). It is built with modularity in mind, featuring well-organized components such as controllers, models, and schemas for scalability and maintainability.

---

## Features

- **Modular Design:** Organized structure with separate directories for controllers, models, and schemas.
- **Environment Management:** `.env` file for configuration and sensitive data.
- **Pre-Commit Hooks:** Configured using `.pre-commit-config.yaml` to enforce code quality.
- **Auto-Generated API Documentation:** Automatically generated interactive API documentation with Swagger UI.
- **Fast and Async:** Built on FastAPI, leveraging Python's `asyncio` for high performance.

---

## Requirements

To run this application, ensure you have the following installed:

- Python 3.9 or above
- `pip` (Python package installer)

---

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/your-repo/coe-poc-backend.git
   cd coe-poc-backend
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

---

## Running the Application

1. Start the FastAPI server:
   ```bash
   uvicorn main:app --port 8002 --reload
   ```

   - Replace `main:app` with your application's entry point (e.g., `app` in `main.py`).

2. Open your browser and navigate to:
   - **Swagger UI:** [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
   - **ReDoc UI:** [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)

---

## Directory Structure

```
coe-poc-backend/
├── controllers/             # Contains route logic for API endpoints
├── models/                  # ORM models (e.g., SQLAlchemy)
├── schemas/                 # Pydantic models for request/response validation
├── .env                     # Environment variables
├── .gitignore               # Git ignore rules
├── .pre-commit-config.yaml  # Pre-commit hooks configuration
├── dummy.json               # Example JSON file
├── main.py                  # Application entry point
├── Makefile                 # Automation tasks
├── package-lock.json        # Dependencies for Node.js (if applicable)
├── README.md                # Project documentation
└── requirements.txt         # Python dependencies
```

---

## API Documentation

The interactive API documentation is available at:

- **Swagger UI:** [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **ReDoc UI:** [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)

These tools allow you to explore and test the API endpoints directly from your browser.

---

## Testing

To run the test suite:

```bash
pytest
```

Ensure all tests pass before deploying.

---

## Contact

For questions, reach out at: [riddhi.singh@in.ey.com](mailto:riddhi.singh@in.ey.com).