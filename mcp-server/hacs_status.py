import json
import re
from pathlib import Path
from typing import Any

import httpx

import server


def version_key(value: Any) -> tuple:
    parts = re.split(r"[.+\-_]", str(value or "").lstrip("vV"))
    return tuple(
        (0, int(match.group(1))) if (match := re.match(r"(\d+)", part)) else (1, part.casefold())
        for part in parts
    )


def _pending_update(repo: dict[str, Any]) -> bool:
    """Mirror HACS' repository pending_update logic for stored repository data."""
    if not repo.get("installed"):
        return False

    selected_tag = repo.get("selected_tag")
    default_branch = repo.get("default_branch")

    if selected_tag is not None and selected_tag == default_branch:
        return bool(repo.get("installed_commit") and repo.get("installed_commit") != repo.get("last_commit"))

    if repo.get("releases"):
        installed = repo.get("installed_version")
        available = repo.get("prerelease") if repo.get("show_beta") and repo.get("prerelease") else repo.get("last_version")
        if installed and available:
            if version_key(available) > version_key(installed):
                return True
            if version_key(available) < version_key(installed):
                return False
        return bool(installed and available and installed != available)

    installed_commit = repo.get("installed_commit")
    last_commit = repo.get("last_commit")
    return bool(installed_commit and last_commit and installed_commit != last_commit)


def _repository_records(value: Any):
    """Recursively find HACS repository records across HACS storage layouts."""
    if isinstance(value, dict):
        if value.get("full_name"):
            yield value
        for child in value.values():
            yield from _repository_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from _repository_records(child)


async def get_hacs_info() -> dict[str, Any]:
    """Return HACS status and installed repository update state from HACS storage."""
    config_root: Path = server.CONFIG_ROOT
    hacs_dir = config_root / "custom_components" / "hacs"
    manifest_path = hacs_dir / "manifest.json"

    if not (hacs_dir.is_dir() and manifest_path.is_file()):
        return {
            "installed": False,
            "version": None,
            "latest_version": None,
            "update_available": False,
            "installed_repository_count": 0,
            "updates_available": 0,
            "installed_repositories": [],
            "message": "HACS is not installed in the connected Home Assistant instance.",
        }

    try:
        version = json.loads(manifest_path.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        version = None

    latest_version = None
    try:
        async with httpx.AsyncClient(timeout=server.HA_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(
                "https://api.github.com/repos/hacs/integration/releases/latest",
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "home-assistant-mcp",
                },
            )
            response.raise_for_status()
            latest_version = str(response.json().get("tag_name") or "") or None
    except (httpx.HTTPError, ValueError):
        pass

    repositories_path = config_root / ".storage" / "hacs.repositories"
    try:
        raw = json.loads(repositories_path.read_text(encoding="utf-8"))
        storage_error = None
    except (OSError, ValueError) as exc:
        raw = None
        storage_error = str(exc)

    installed: list[dict[str, Any]] = []
    seen: set[str] = set()

    if raw is not None:
        for repo in _repository_records(raw):
            full_name = str(repo.get("full_name") or "")
            if not full_name or full_name.casefold() == "hacs/integration" or full_name in seen:
                continue

            category = repo.get("category")
            domain = repo.get("domain")
            local_path = repo.get("local_path")
            installed_flag = bool(repo.get("installed"))

            # Be compatible with HACS storage variants that do not persist
            # the installed flag: verify the local content on disk.
            if not installed_flag:
                if category == "integration" and domain:
                    installed_flag = (config_root / "custom_components" / str(domain)).is_dir()
                elif local_path:
                    try:
                        installed_flag = Path(str(local_path)).expanduser().exists()
                    except OSError:
                        installed_flag = False

            if not installed_flag:
                continue

            update_available = _pending_update(repo)
            installed_version = repo.get("installed_version")
            available_version = (
                repo.get("prerelease")
                if repo.get("show_beta") and repo.get("prerelease")
                else repo.get("last_version")
            )

            installed.append({
                "full_name": full_name,
                "name": repo.get("name") or (domain if category == "integration" else full_name.rsplit("/", 1)[-1]),
                "category": category,
                "domain": domain,
                "description": repo.get("description"),
                "installed": True,
                "installed_version": installed_version,
                "available_version": available_version,
                "installed_commit": repo.get("installed_commit"),
                "available_commit": repo.get("last_commit"),
                "releases": bool(repo.get("releases")),
                "update_available": update_available,
                "status": "update_available" if update_available else "current",
                "selected_tag": repo.get("selected_tag"),
                "default_branch": repo.get("default_branch"),
            })
            seen.add(full_name)

    installed.sort(key=lambda item: str(item.get("name") or item.get("full_name") or "").casefold())
    updates = sum(1 for item in installed if item["update_available"])

    return {
        "installed": True,
        "version": version,
        "latest_version": latest_version,
        "update_available": bool(version and latest_version and version_key(latest_version) > version_key(version)),
        "installed_repository_count": len(installed),
        "updates_available": updates,
        "installed_repositories": installed,
        "source": "hacs_storage",
        "storage_error": storage_error,
        "message": "HACS is installed. Repository status is evaluated using HACS' own stored version/commit data.",
    }
