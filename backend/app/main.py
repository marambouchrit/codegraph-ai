"""FastAPI application entry point.

Run with:  uvicorn app.main:app --reload
"""

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import health, projects
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import setup_logging


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="GraphRAG-powered codebase analysis assistant.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, error: AppError) -> JSONResponse:
        # Same {"detail": ...} shape as FastAPI's own HTTP errors.
        return JSONResponse(status_code=error.status_code, content={"detail": error.message})

    app.include_router(health.router)
    app.include_router(projects.router)

    logging.getLogger(__name__).info("%s started (%s)", settings.app_name, settings.environment)
    return app


app = create_app()
