"""Project endpoints: import a repository (GitHub or ZIP), list, inspect and delete projects."""

from fastapi import APIRouter, Depends, File, UploadFile

from app.api.dependencies import get_project_service
from app.schemas.project import GitHubProjectCreate, Project, ProjectFiles, SourceFile
from app.services.project_service import ProjectService

router = APIRouter(prefix="/projects", tags=["projects"])

# Routes are plain `def` functions: cloning and extracting are blocking operations,
# and FastAPI runs `def` routes in a thread pool so they do not block the server.


@router.post("/github", response_model=Project, status_code=201)
def create_project_from_github(
    request: GitHubProjectCreate,
    service: ProjectService = Depends(get_project_service),
) -> Project:
    return service.create_from_github(request.url)


@router.post("/zip", response_model=Project, status_code=201)
def create_project_from_zip(
    file: UploadFile = File(description="ZIP archive of the project"),
    service: ProjectService = Depends(get_project_service),
) -> Project:
    return service.create_from_zip(file.file, file.filename or "upload.zip")


@router.get("", response_model=list[Project])
def list_projects(service: ProjectService = Depends(get_project_service)) -> list[Project]:
    return service.list_projects()


@router.get("/{project_id}", response_model=Project)
def get_project(
    project_id: str, service: ProjectService = Depends(get_project_service)
) -> Project:
    return service.get_project(project_id)


@router.get("/{project_id}/files", response_model=ProjectFiles)
def list_project_files(
    project_id: str, service: ProjectService = Depends(get_project_service)
) -> ProjectFiles:
    files = service.list_files(project_id)
    return ProjectFiles(
        project_id=project_id,
        files=[
            SourceFile(path=file.path, language=file.language, size_bytes=file.size_bytes)
            for file in files
        ],
    )


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str, service: ProjectService = Depends(get_project_service)) -> None:
    service.delete_project(project_id)
