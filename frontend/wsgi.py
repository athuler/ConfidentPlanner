"""WSGI entry point for production servers (gunicorn on Cloud Run):  gunicorn frontend.wsgi:app"""
import logging

from .app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
app = create_app()
