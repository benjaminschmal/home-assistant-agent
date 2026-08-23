from pathlib import Path
import re

server_path=Path('mcp-server/server.py')
agent_path=Path('agent/agent.py')
workflow_path=Path('.github/workflows/docker-publish.yml')
server=server_path.read_text()
agent=agent_path.read_text()

if 'async def get_hacs_info' not in server:
    marker='\n\nasync def list_config_entries() -> list[dict[str, Any]]:'
    func='''\n\nasync def get_hacs_info() -> dict[str, Any]:\n    """Inspect the optional HACS installation without assuming HA OS or Supervisor."""\n    hacs_dir = CONFIG_ROOT / "custom_components" / "hacs"\n    manifest_path = hacs_dir / "manifest.json"\n    storage_dir = CONFIG_ROOT / ".storage"\n    hacs_storage_path = storage_dir / "hacs.hacs"\n    repositories_path = storage_dir / "hacs.repositories"\n    if not (hacs_dir.is_dir() and manifest_path.is_file()):\n        return {"installed": False, "version": None, "latest_version": None, "update_available": False, "installed_repositories": [], "installed_repository_count": 0, "message": "HACS is not installed in the connected Home Assistant configuration directory."}\n    version = None\n    try:\n        version = json.loads(manifest_path.read_text(encoding="utf-8")).get("version")\n    except (OSError, ValueError):\n        pass\n    try:\n        raw=json.loads(hacs_storage_path.read_text(encoding="utf-8")); data=raw.get("data",raw) if isinstance(raw,dict) else {}\n        version=version or data.get("version")\n    except (OSError, ValueError):\n        pass\n    repositories=[]\n    try:\n        raw=json.loads(repositories_path.read_text(encoding="utf-8")); data=raw.get("data",raw) if isinstance(raw,dict) else {}\n        for repo in data.get("repositories",[]) if isinstance(data,dict) else []:\n            if not isinstance(repo,dict): continue\n            d=repo.get("data",{}) if isinstance(repo.get("data"),dict) else {}\n            if not bool(d.get("installed",repo.get("installed",False))): continue\n            repositories.append({k:v for k,v in {"full_name":d.get("full_name") or repo.get("full_name"),"name":d.get("name") or repo.get("name") or d.get("manifest_name"),"category":d.get("category") or repo.get("category"),"installed_version":d.get("installed_version") or repo.get("installed_version"),"latest_version":d.get("last_version") or repo.get("last_version"),"pending_restart":bool(d.get("pending_restart",repo.get("pending_restart",False)))}.items() if v not in (None,"")})\n    except (OSError, ValueError):\n        pass\n    def vk(v):\n        out=[]\n        for p in re.split(r"[.+\\-_]",str(v or "").lstrip("vV")):\n            m=re.match(r"(\\d+)",p); out.append((0,int(m.group(1))) if m else (1,p.casefold()))\n        return tuple(out)\n    latest_version=None; latest_url="https://github.com/hacs/integration/releases/latest"\n    try:\n        async with httpx.AsyncClient(timeout=HA_TIMEOUT,follow_redirects=True) as client:\n            r=await client.get("https://api.github.com/repos/hacs/integration/releases/latest",headers={"Accept":"application/vnd.github+json","User-Agent":"home-assistant-mcp"}); r.raise_for_status(); release=r.json(); latest_version=str(release.get("tag_name") or "").strip() or None; latest_url=str(release.get("html_url") or latest_url)\n    except (httpx.HTTPError,ValueError):\n        pass\n    update_available=bool(version and latest_version and vk(latest_version)>vk(version))\n    repo_updates=[]\n    for repo in repositories:\n        iv,lv=repo.get("installed_version"),repo.get("latest_version"); repo["update_available"]=bool(iv and lv and vk(lv)>vk(iv))\n        if repo["update_available"]: repo_updates.append(repo.get("full_name") or repo.get("name"))\n    return {"installed":True,"version":version,"latest_version":latest_version,"update_available":update_available,"latest_release_url":latest_url,"installed_repository_count":len(repositories),"repositories_with_updates":repo_updates,"installed_repositories":repositories,"storage_detected":{"hacs_hacs":hacs_storage_path.is_file(),"hacs_repositories":repositories_path.is_file()}}\n'''
    if marker not in server: raise SystemExit('server marker missing')
    server=server.replace(marker,func+marker,1)
    marker='        Tool(name="get_energy_info", description="Read Home Assistant Energy Dashboard metadata.", inputSchema={"type": "object", "properties": {}}),'
    if marker not in server: raise SystemExit('tool marker missing')
    server=server.replace(marker,marker+'\n        Tool(name="get_hacs_info", description="Inspect HACS installation, version, installed repositories and available updates without assuming Home Assistant OS or Supervisor.", inputSchema={"type": "object", "properties": {}}),',1)
    marker='    if params.name == "get_energy_info":\n        return text_result(await get_energy_info())\n'
    if marker not in server: raise SystemExit('call marker missing')
    server=server.replace(marker,marker+'    if params.name == "get_hacs_info":\n        return text_result(await get_hacs_info())\n',1)
    server_path.write_text(server)

if 'get_hacs_info' not in agent:
    marker='When comparing versions, distinguish stable releases from beta/development releases.\")'
    replacement='When comparing versions, distinguish stable releases from beta/development releases. For questions about HACS, always call get_hacs_info first. Do not assume HACS is installed and do not use Supervisor or the Add-on Store as a proxy for HACS. Use the returned HACS version, latest stable version, installed repository count, categories and update information. If HACS or its repository storage cannot be detected, report that limitation instead of guessing.\")'
    if marker not in agent: raise SystemExit('agent marker missing')
    agent_path.write_text(agent.replace(marker,replacement,1))

text=workflow_path.read_text()
start=text.find('      - name: Apply HACS patch\n')
if start>=0:
    end=text.find('      - name: Extract Docker metadata for agent\n',start)
    if end<0: raise SystemExit('workflow cleanup marker missing')
    text=text[:start]+text[end:]
    workflow_path.write_text(text)

Path('scripts/patch_hacs.py').unlink(missing_ok=True)
print('HACS patch applied')
