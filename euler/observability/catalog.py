"""An explicit server-owned project directory; never accept paths from a browser."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import hashlib


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    path: Path
    config: Path | None
    description: str = ""


def catalog(default: Path) -> tuple[list[Project], str]:
    config = os.environ.get("EULER_DASHBOARD_CONFIG")
    config = Path(config).resolve() if config else None
    settings = json.loads(config.read_text()) if config else {}
    default = default.resolve()
    registry = settings.get("project_catalog")
    projects = []
    data = {}
    if registry:
        path = Path(registry)
        path = path if path.is_absolute() else config.parent / path
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("project catalog too large")
        data = json.loads(path.read_text())
        def relative(value):
            p = Path(value)
            return (p if p.is_absolute() else path.parent / p).resolve()
        entries = list(data.get("projects", [])[:32])
        restored = os.environ.get('EULER_MILESTONE_REGISTRY')
        if restored and Path(restored).is_file():
            entries += json.loads(Path(restored).read_text()).get('projects', [])[-32:]
        for entry in entries:
            identifier = entry.get("id", "")
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}", identifier):
                raise ValueError("invalid project identifier")
            projects.append(Project(identifier, str(entry.get("name", identifier))[:160],
                                    relative(entry["project_dir"]),
                                    relative(entry["config"]) if entry.get("config") else None,
                                    str(entry.get("description", ""))[:300]))
        # Only shallow, explicitly configured research containers are searched.
        # Discovery never adopts a project's arbitrary external-source config.
        containers = [relative(p) for p in data.get("discover_project_roots", [])[:16]]
        for workspace in data.get("discover_workspaces", [])[:8]:
            workspace = relative(workspace)
            if workspace.is_dir():
                containers.append(workspace / "runtime/projects")
                containers += [p / "runtime/projects" for p in sorted(workspace.iterdir())[:128]
                               if p.is_dir() and not p.is_symlink() and not p.name.startswith('.')]
        for container in containers:
            if not container.is_dir() or container.is_symlink():
                continue
            for folder in sorted(container.iterdir())[:128]:
                if len(projects) >= 64:
                    break
                meta = folder / "project.json"
                if (not folder.is_dir() or folder.is_symlink() or meta.is_symlink()
                        or any(p.path == folder.resolve() for p in projects)):
                    continue
                try:
                    if meta.stat().st_size > 256 * 1024:
                        continue
                    metadata = json.loads(meta.read_text())
                    if not isinstance(metadata, dict) or not isinstance(metadata.get("workers"), list):
                        continue
                except (OSError, ValueError):
                    continue
                identifier = re.sub(r"[^a-zA-Z0-9_-]", "-", folder.name)[:70].strip('-') or 'project'
                if any(p.id == identifier for p in projects):
                    identifier += '-' + hashlib.sha256(str(folder).encode()).hexdigest()[:8]
                projects.append(Project(identifier, str(metadata.get("name") or folder.name)[:160],
                                        folder.resolve(), None, "研究工作区 · 自动发现"))
    creation = os.environ.get('EULER_PROJECT_CREATION_CONFIG')
    if creation:
        managed = Path(json.loads(Path(creation).read_text())['registry'])
        if managed.is_file():
            if managed.stat().st_size > 1024 * 1024:raise ValueError('managed project registry too large')
            for entry in json.loads(managed.read_text()).get('projects',[]):
                if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}',entry['id']):raise ValueError('invalid project identifier')
                projects.append(Project(entry['id'],str(entry['name'])[:160],Path(entry['project_dir']).resolve(),
                    Path(entry['config']).resolve() if entry.get('config') else None,str(entry.get('description',''))[:300]))
    projects = [Project(p.id, p.name, p.path, config, p.description) if p.path == default and p.config is None and config else p for p in projects]
    selected = next((p.id for p in projects if p.path == default), None)
    if selected is None:
        selected = default.name
        projects.insert(0, Project(selected, str(settings.get("name", selected)), default, config,
                                   str(settings.get("description", ""))[:300]))
    if len({p.id for p in projects}) != len(projects):
        raise ValueError("duplicate project identifier")
    return projects, selected


def resolve(default: Path, identifier: str | None) -> Project:
    projects, selected = catalog(default)
    match = next((p for p in projects if p.id == (identifier or selected)), None)
    if match is None:
        raise KeyError("unknown project")
    return match
