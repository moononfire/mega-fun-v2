import asyncio
import time
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from config import CLIENTS_DIR, SCRIPTS_DIR, VPS_WORKER_TOKEN
from worker.db import update_run_pid, finish_run


async def execute_run(
    vps_run_id: str,
    run_id: str,
    client_slug: str,
    script: str,
    params: dict,
    webhook_url: str,
):
    log_path = Path(CLIENTS_DIR) / client_slug / "logs" / f"{vps_run_id}.log"
    output_dir = Path(CLIENTS_DIR) / client_slug / "output"
    start_time = time.time()
    output_files: list[str] = []

    args = ["python3", str(Path(SCRIPTS_DIR) / f"{script}.py"), client_slug]
    for key, value in params.items():
        args.append(f"{key}={value}")

    try:
        with open(log_path, "w") as log_file:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(Path(CLIENTS_DIR) / client_slug),
            )
            update_run_pid(vps_run_id, proc.pid)
            await proc.wait()

        if proc.returncode == 0:
            output_files = [
                f.name for f in output_dir.iterdir()
                if f.is_file() and f.stat().st_mtime > start_time
            ]
            status, error_msg = "done", None
        else:
            status, error_msg = "error", f"Exit code: {proc.returncode}"
    except Exception as e:
        status, error_msg = "error", str(e)

    finish_run(vps_run_id, status, output_files, error_msg)
    await _notify_nextjs(webhook_url, run_id, vps_run_id, status, output_files, error_msg)


async def _notify_nextjs(
    webhook_url: str,
    run_id: str,
    vps_run_id: str,
    status: str,
    output_files: list[str],
    error_msg: str | None,
):
    payload = {
        "runId": run_id,
        "vpsRunId": vps_run_id,
        "status": status,
        "outputFiles": output_files,
        "errorMessage": error_msg,
        "finishedAt": datetime.now(timezone.utc).isoformat(),
    }
    for attempt in range(3):
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    webhook_url,
                    json=payload,
                    headers={"Authorization": f"Bearer {VPS_WORKER_TOKEN}"},
                    timeout=30,
                )
                if r.status_code == 200:
                    return
                print(f"[notify] attempt {attempt+1}: webhook zwrócił {r.status_code}, retry…", flush=True)
        except Exception as e:
            print(f"[notify] attempt {attempt+1}: wyjątek {e}, retry…", flush=True)
        await asyncio.sleep(2 ** attempt)
