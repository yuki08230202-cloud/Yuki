"""uvicorn エントリポイント:  uvicorn enpra.main:app --host 0.0.0.0 --port $PORT"""
import logging

from .jobs import *  # noqa: F401,F403 - executor 登録
from .web import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
