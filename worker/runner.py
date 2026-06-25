import asyncio
import sys
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

    args = [sys.executable, str(Path(SCRIPTS_DIR) / f"{script}.py"), client_slug]
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
            all_files = list(output_dir.iterdir()) if output_dir.exists() else []
            output_files = [
                f.name for f in all_files
                if f.is_file() and f.stat().st_mtime >= start_time
            ]
            print(f"[runner] returncode=0 output_dir={output_dir} all_files={[f.name for f in all_files]} new_files={output_files} start_time={start_time}", flush=True)
            status, error_msg = "done", None
        else:
            print(f"[runner] returncode={proc.returncode} — error", flush=True)
            status, error_msg = "error", f"Exit code: {proc.returncode}"
    except Exception as e:
        print(f"[runner] EXCEPTION: {e}", flush=True)
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
    print(f"[notify] SEND run_id={run_id} status={status} output_files={output_files} url={webhook_url}", flush=True)
    for attempt in range(3):
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    webhook_url,
                    json=payload,
                    headers={"Authorization": f"Bearer {VPS_WORKER_TOKEN}"},
                    timeout=30,
                )
                print(f"[notify] attempt={attempt+1} HTTP={r.status_code} body={r.text[:300]}", flush=True)
                if r.status_code == 200:
                    return
        except Exception as e:
            print(f"[notify] attempt={attempt+1} EXCEPTION={e}", flush=True)
        await asyncio.sleep(2 ** attempt)
    print(f"[notify] FAILED all 3 attempts for run_id={run_id}", flush=True)
