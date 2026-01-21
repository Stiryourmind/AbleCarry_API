import os
import time
import json
import base64
import secrets
import mimetypes
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, UploadFile, File, Form, Query
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# ---------------- CONFIG ----------------

BASE_URL = "https://www.runninghub.ai"

RUNNINGHUB_API_KEY = os.getenv("RUNNINGHUB_API_KEY")
WORKFLOW_ID = (os.getenv("RUNNINGHUB_WORKFLOW_ID") or "").split("?")[0]

USER_NODE_ID = os.getenv("RUNNINGHUB_USER_NODE_ID", "97")
USER_FIELD = os.getenv("RUNNINGHUB_USER_FIELD_NAME", "data")

SWITCH_NODE_ID = os.getenv("RUNNINGHUB_SWITCH_NODE_ID", "101")
SWITCH_FIELD = os.getenv("RUNNINGHUB_SWITCH_FIELD_NAME", "Path")

PROMPT_NODE_ID = os.getenv("RUNNINGHUB_PROMPT_NODE_ID", "86")
PROMPT_FIELD = os.getenv("RUNNINGHUB_PROMPT_FIELD_NAME", "text")

SEED_NODE_ID = os.getenv("RUNNINGHUB_SEED_NODE_ID", "50")
SEED_FIELD = os.getenv("RUNNINGHUB_SEED_FIELD_NAME", "seed")

FIXED_PROMPT = "圖一的人背著袋以圖三的姿勢垂直飄浮在空中, 雙手垂放在身旁,雙腳離地垂下,背景是灰色空間, 地下有飄浮陰影, 保持圖一人物的樣貌和衣著不變。"
MAX_SEED = 2147483647

ARCHIVE_TOKEN = os.getenv("ARCHIVE_TOKEN", "")
ARCHIVE_RETENTION_DAYS = os.getenv("ARCHIVE_RETENTION_DAYS", "").strip()

ARCHIVE_DIR = Path("archives")
ARCHIVE_INPUT_DIR = ARCHIVE_DIR / "input"
ARCHIVE_OUTPUT_DIR = ARCHIVE_DIR / "output"
ARCHIVE_LOG = ARCHIVE_DIR / "archives.jsonl"

if not RUNNINGHUB_API_KEY or not WORKFLOW_ID:
    raise RuntimeError("Missing RUNNINGHUB_API_KEY or RUNNINGHUB_WORKFLOW_ID")

# ---------------- APP ----------------

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten later if needed
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="public"), name="static")

@app.get("/")
def home():
    return FileResponse("public/index.html")

@app.get("/healthz")
def healthz():
    return "ok"

# ---------------- Helpers ----------------

def utc_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def random_seed() -> int:
    return secrets.randbelow(MAX_SEED + 1)

def bytes_to_base64(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")

def rh_post(endpoint: str, payload: dict):
    r = requests.post(
        f"{BASE_URL}{endpoint}",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()

def create_task(node_info_list):
    resp = rh_post("/task/openapi/create", {
        "apiKey": RUNNINGHUB_API_KEY,
        "workflowId": WORKFLOW_ID,
        "nodeInfoList": node_info_list,
        "addMetadata": True,
        "instanceType": "plus",
        "usePersonalQueue": "true",
    })
    if resp.get("code") != 0:
        raise RuntimeError(f"Create error: {resp}")

    task_id = resp.get("data", {}).get("taskId") or resp.get("data", {}).get("id")
    if not task_id:
        raise RuntimeError(f"Create succeeded but no taskId: {resp}")
    return str(task_id)

def poll_outputs(task_id: str, timeout_sec: int = 300, interval_sec: int = 3):
    start = time.time()
    while time.time() - start < timeout_sec:
        resp = rh_post("/task/openapi/outputs", {"apiKey": RUNNINGHUB_API_KEY, "taskId": task_id})
        code = resp.get("code")

        if code == 0:
            data = resp.get("data", [])
            if isinstance(data, list) and data:
                return data
            if isinstance(data, dict) and isinstance(data.get("outputs"), list) and data["outputs"]:
                return data["outputs"]
            raise RuntimeError(f"Outputs empty: {resp}")

        if code == 804:
            time.sleep(interval_sec)
            continue

        raise RuntimeError(f"Outputs error: {resp}")

    raise TimeoutError("RunningHub timeout")

def pick_image_url(outputs):
    first = outputs[0] if outputs else {}
    return first.get("fileUrl") or first.get("imageUrl") or first.get("url") or first.get("image_url")

def require_token(token: str):
    if not ARCHIVE_TOKEN:
        # If you didn't set ARCHIVE_TOKEN, we still block listing/downloading.
        raise RuntimeError("ARCHIVE_TOKEN is not configured on server.")
    if token != ARCHIVE_TOKEN:
        raise RuntimeError("Invalid token.")

def ensure_archive_dirs():
    ARCHIVE_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

def safe_ext_from_mime(mime: str) -> str:
    ext = mimetypes.guess_extension(mime or "")
    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return ext
    return ".bin"

def cleanup_old_archives():
    """Optional retention cleanup (best-effort)."""
    if not ARCHIVE_RETENTION_DAYS:
        return
    try:
        days = int(ARCHIVE_RETENTION_DAYS)
        if days <= 0:
            return
    except:
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Delete files older than cutoff based on filename timestamp prefix.
    # Our filenames start with UTC stamp: YYYYMMDDTHHMMSSZ_...
    for folder in [ARCHIVE_INPUT_DIR, ARCHIVE_OUTPUT_DIR]:
        for p in folder.glob("*"):
            try:
                stamp = p.name.split("_", 1)[0]
                dt = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    p.unlink(missing_ok=True)
            except:
                continue

    # Compact log is not handled here; leaving it as audit trail.

# ---------------- API ----------------

@app.post("/api/generate")
async def generate(
    userImage: UploadFile = File(...),
    productOption: str = Form(...),  # "1" or "2"
):
    ensure_archive_dirs()
    cleanup_old_archives()

    # validate option
    try:
        opt = int(productOption)
    except:
        return JSONResponse({"error": "productOption must be a number (e.g. 1 or 2)."}, status_code=400)
    if opt < 1:
        return JSONResponse({"error": "productOption must be >= 1."}, status_code=400)

    # read input image
    input_bytes = await userImage.read()
    if not input_bytes:
        return JSONResponse({"error": "Empty upload."}, status_code=400)

    # base64 for LoadImageFromBase64 node (raw base64 only)
    user_b64 = bytes_to_base64(input_bytes)
    user_b64 = "".join(user_b64.split())
    missing = len(user_b64) % 4
    if missing:
        user_b64 += "=" * (4 - missing)

    seed_val = random_seed()

    try:
        node_info_list = [
            {"nodeId": PROMPT_NODE_ID, "fieldName": PROMPT_FIELD, "fieldValue": FIXED_PROMPT},
            {"nodeId": USER_NODE_ID, "fieldName": USER_FIELD, "fieldValue": user_b64},
            {"nodeId": SWITCH_NODE_ID, "fieldName": SWITCH_FIELD, "fieldValue": str(opt)},
            {"nodeId": SEED_NODE_ID, "fieldName": SEED_FIELD, "fieldValue": str(seed_val)},
        ]

        task_id = create_task(node_info_list)
        outputs = poll_outputs(task_id)
        output_url = pick_image_url(outputs)
        if not output_url:
            raise RuntimeError(f"No imageUrl found in outputs: {outputs}")

        # Download output image bytes so we can (a) archive it (b) serve it from our domain for reliable download
        out_resp = requests.get(output_url, timeout=120)
        out_resp.raise_for_status()
        output_bytes = out_resp.content

        # -------- Archive files on Render disk --------
        ts = utc_stamp()
        bag_label = f"bag-{opt:02d}"

        in_mime = userImage.content_type or "application/octet-stream"
        in_ext = safe_ext_from_mime(in_mime)
        input_filename = f"{ts}_{bag_label}_task_{task_id}_input{in_ext}"
        output_filename = f"{ts}_{bag_label}_task_{task_id}_output.png"

        input_path = ARCHIVE_INPUT_DIR / input_filename
        output_path = ARCHIVE_OUTPUT_DIR / output_filename

        input_path.write_bytes(input_bytes)
        output_path.write_bytes(output_bytes)

        archive_id = f"{ts}_{bag_label}_task_{task_id}"

        # Write log entry (JSONL)
        record = {
            "id": archive_id,
            "ts": ts,
            "taskId": task_id,
            "productOption": opt,
            "input": {
                "filename": input_filename,
                "mime": in_mime,
                "size": len(input_bytes),
            },
            "output": {
                "filename": output_filename,
                "mime": "image/png",
                "size": len(output_bytes),
                "runninghubUrl": output_url,
            },
        }
        with ARCHIVE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        # Important:
        # Return output download URL served from OUR domain (reliable download attribute)
        return {
            "taskId": task_id,
            "seed": seed_val,
            "archiveId": archive_id,
            "imageUrl": f"/api/archive/download/{archive_id}?token={ARCHIVE_TOKEN}&kind=output",
            "inputDownloadUrl": f"/api/archive/download/{archive_id}?token={ARCHIVE_TOKEN}&kind=input",
        }

    except Exception as e:
        print("ERROR /api/generate:", repr(e))
        return JSONResponse({"error": str(e), "type": e.__class__.__name__}, status_code=500)

# -------- Archive APIs (protected by token) --------

@app.get("/api/archive/list")
def archive_list(
    token: str = Query(""),
    since: Optional[str] = Query(None),  # optional ISO-ish stamp filter
    limit: int = Query(50, ge=1, le=500),
):
    try:
        require_token(token)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=401)

    if not ARCHIVE_LOG.exists():
        return {"items": []}

    items = []
    with ARCHIVE_LOG.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except:
                continue
            if since and rec.get("ts", "") <= since:
                continue
            items.append(rec)

    # newest last in file; return last N
    items = items[-limit:]
    return {"items": items}

@app.get("/api/archive/download/{archive_id}")
def archive_download(
    archive_id: str,
    token: str = Query(""),
    kind: str = Query("output"),  # input|output
):
    try:
        require_token(token)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=401)

    ensure_archive_dirs()

    if kind not in ("input", "output"):
        return JSONResponse({"error": "kind must be input or output"}, status_code=400)

    # Find file by scanning the folders with prefix = archive_id
    folder = ARCHIVE_INPUT_DIR if kind == "input" else ARCHIVE_OUTPUT_DIR
    matches = list(folder.glob(f"{archive_id}_*"))
    if not matches:
        return JSONResponse({"error": "File not found"}, status_code=404)

    path = matches[0]
    mime = "application/octet-stream"
    if kind == "output":
        mime = "image/png"

    def iterfile():
        with path.open("rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk

    headers = {"Content-Disposition": f'attachment; filename="{path.name}"'}
    return StreamingResponse(iterfile(), media_type=mime, headers=headers)
