import importlib.resources as pkg_resources
from pathlib import Path
from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict


def find_project_root() -> Path:
    """Find the project root by looking for .env or pyproject.toml in cwd and parents."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / ".env").exists() or (parent / "pyproject.toml").exists():
            return parent
    return cwd


def get_resource_path(subpath: str) -> Path:
    """Resolve bundled resource path with fallback to package/project directory."""
    try:
        ref = pkg_resources.files("leetnotes_core.resources").joinpath(subpath)
        p = Path(str(ref))
        if p.exists():
            return p
    except TypeError, FileNotFoundError, ModuleNotFoundError, AttributeError:
        pass

    pkg_path = Path(__file__).resolve().parent / "resources" / subpath
    if pkg_path.exists():
        return pkg_path

    return find_project_root() / "resources" / subpath


class BaseProjectSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    PROJECT_ROOT_DIR: ClassVar[Path] = find_project_root()
    PACKAGE_DIR: ClassVar[Path] = Path(__file__).resolve().parent
