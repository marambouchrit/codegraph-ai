"""Shared FastAPI dependencies.

Routes ask for services with `Depends(...)`. Tests can replace them through
`app.dependency_overrides`, for example to use a temporary workspace.
"""

from fastapi import Depends

from app.core.config import Settings, get_settings
from app.services.project_service import ProjectService


def get_project_service(settings: Settings = Depends(get_settings)) -> ProjectService:
    return ProjectService(settings)
