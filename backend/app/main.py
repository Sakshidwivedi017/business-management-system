import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.api.chat import agent_runtime
from app.config import get_settings
from app.db.connection import dispose_engine

settings = get_settings()

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Starting %s (%s)", settings.app_name, settings.app_env)
    yield
    agent_runtime.close()
    dispose_engine()
    logger.info("Shutdown complete")


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
