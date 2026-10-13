"""ASGI entry point: ``uvicorn crosspost.main:app``."""
from .web.app import create_app

app = create_app()
