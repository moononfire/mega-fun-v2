import json
import re
import time
import asyncio
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.responses import StreamingResponse, PlainTextResponse
from pydantic import BaseModel

from config import VPS_WORKER_TOKEN, CLIENTS_DIR, SCRIPTS_DIR
from worker.db import (
    init_db, get_client, insert_client, update_client_status, delete_client,
    insert_run, get_run, count_active_runs, count_all_active_runs, count_active_clients,
)
from worker.runner import execute_run

app = FastAPI(title="Mega-Fun VPS Worker")
_start_time = time.time()

SLUG_RE = re.compile(r"^[a-z0-9-]+$")


@app.on_event("startup")
def on_startup():
    init_db()


# ── Auth ─────────────────────────────────────────────────────────────────────

def verify_token(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != VPS_WORKER_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", dependencies=[Depends(verify_token)])
def health():
    import shutil
    usage = shutil.disk_usage("/")
    return {
        "status": "ok",
        "diskUsedGB": round((usage.total - usage.free) / 1e9, 2),
        "diskFreeGB": round(usage.free / 1e9, 2),
        "activeClients": count_active_clients(),
        "runningScripts": count_all_active_runs(),
        "uptimeSeconds": int(time.time() - _start_time),
    }


# ── Clients ───────────────────────────────────────────────────────────────────

class ClientCreate(BaseModel):
    slug: str
    name: str = ""
    config: dict = {}


class ClientPatch(BaseModel):
    status: str | None = None
    config: dict | None = None


@app.post("/clients", status_code=201, dependencies=[Depends(verify_token)])
def create_client(body: ClientCreate):
    if not SLUG_RE.match(body.slug):
        raise HTTPException(status_code=400, detail="Invalid slug — only [a-z0-9-] allowed")
    if get_client(body.slug):
        raise HTTPException(status_code=409, detail="Client already exists")

    base = Path(CLIENTS_DIR) / body.slug
    for sub in ("input", "output", "logs"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    (base / "config.json").write_text(json.dumps(body.config, indent=2))

    insert_client(body.slug)
    return {"ok": True, "slug": body.slug}


@app.delete("/clients/{slug}", dependencies=[Depends(verify_token)])
def delete_client_endpoint(slug: str):
    client = get_client(slug)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    if count_active_runs(slug) > 0:
        raise HTTPException(status_code=409, detail="Client has active runs")

    import shutil
    base = Path(CLIENTS_DIR) / slug
    if base.exists():
        shutil.rmtree(base)
    delete_client(slug)
    return {"ok": True}


@app.patch("/clients/{slug}", dependencies=[Depends(verify_token)])
def patch_client(slug: str, body: ClientPatch):
    client = get_client(slug)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    if body.status:
        update_client_status(slug, body.status)
    if body.config is not None:
        config_path = Path(CLIENTS_DIR) / slug / "config.json"
        config_path.write_text(json.dumps(body.config, indent=2))
    return {"ok": True}


# ── Runs ──────────────────────────────────────────────────────────────────────

class RunCreate(BaseModel):
    runId: str
    clientSlug: str
    script: str
    params: dict = {}
    webhookUrl: str


@app.post("/run", status_code=202, dependencies=[Depends(verify_token)])
async def start_run(body: RunCreate):
    client = get_client(body.clientSlug)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    if client["status"] == "suspended":
        raise HTTPException(status_code=403, detail="Client is suspended")

    script_path = Path(SCRIPTS_DIR) / f"{body.script}.py"
    if not script_path.exists():
        raise HTTPException(status_code=400, detail=f"Script '{body.script}' not found")

    vps_run_id = str(uuid.uuid4())
    log_path = str(Path(CLIENTS_DIR) / body.clientSlug / "logs" / f"{vps_run_id}.log")
    insert_run(vps_run_id, body.runId, body.clientSlug, body.script, log_path)

    asyncio.create_task(execute_run(
        vps_run_id, body.runId, body.clientSlug,
        body.script, body.params, body.webhookUrl,
    ))

    return {"vpsRunId": vps_run_id}


@app.get("/runs/{vps_run_id}", dependencies=[Depends(verify_token)])
def get_run_status(vps_run_id: str):
    run = get_run(vps_run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return dict(run)


@app.get("/runs/{vps_run_id}/logs", dependencies=[Depends(verify_token)])
def get_run_logs(vps_run_id: str, tail: int = 0):
    run = get_run(vps_run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    log_path = Path(run["log_path"])
    if not log_path.exists():
        return PlainTextResponse("")
    lines = log_path.read_text(errors="replace").splitlines()
    if tail > 0:
        lines = lines[-tail:]
    return PlainTextResponse("\n".join(lines))


# ── Files ─────────────────────────────────────────────────────────────────────

@app.get("/clients/{slug}/files", dependencies=[Depends(verify_token)])
def list_files(slug: str):
    client = get_client(slug)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    output_dir = Path(CLIENTS_DIR) / slug / "output"
    files = []
    if output_dir.exists():
        for f in sorted(output_dir.iterdir()):
            if f.is_file():
                stat = f.stat()
                files.append({
                    "name": f.name,
                    "sizeBytes": stat.st_size,
                    "modifiedAt": stat.st_mtime,
                })
    return files


@app.get("/clients/{slug}/files/{filename}", dependencies=[Depends(verify_token)])
def download_file(slug: str, filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    file_path = Path(CLIENTS_DIR) / slug / "output" / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    def iter_file():
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    return StreamingResponse(
        iter_file(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/clients/{slug}/files/{filename}", dependencies=[Depends(verify_token)])
def delete_file(slug: str, filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    file_path = Path(CLIENTS_DIR) / slug / "output" / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    file_path.unlink()
    return {"ok": True}
