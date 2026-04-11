# Luciel Backend

FastAPI starter skeleton for Luciel Core MVP.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
cp .env.example .env
uvicorn app.main:app --reload
```

## Initial endpoints
- `GET /health`
- `GET /api/v1/version`
- `POST /api/v1/chat`
- `POST /api/v1/sessions`
- `GET /api/v1/sessions/{session_id}`
