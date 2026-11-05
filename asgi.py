"""Vercel's FastAPI entrypoint for the website and API."""
from app.main import create_app

app = create_app()
