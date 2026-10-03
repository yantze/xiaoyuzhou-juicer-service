"""Vercel's FastAPI entrypoint. Railway may still use the existing factory."""
from app.main import create_app

app = create_app()
