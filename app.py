import os
import time
import base64
import mimetypes
import secrets
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import requests

# ---------------- CONFIG ----------------

BASE_URL = "https://www.runninghub.ai"

RUNNINGHUB_API_KEY = os.getenv("RUNNINGHUB_API_KEY")
WORKFLOW_ID = (os.getenv("RUNNINGHUB_WORKFLOW_ID") or "").split("?")[0]

# User image node (LoadImageFromBase64)
USER_NODE_ID = os.getenv("RUNNINGHUB_USER_NODE_ID", "97")
USER_FIELD = os.getenv("RUNNINGHUB_USER_FIELD_NAME", "data")

# Product selector node (Switch)
SWITCH_NODE_ID = os.getenv("RUNNINGHUB_SWITCH_NODE_ID", "101")
SWITCH_FIELD = os.getenv("RUNNINGHUB_SWITCH_FIELD_NAME", "Path")

# Prompt node (Text)
PROMPT_NODE_ID = os.getenv("RUNNINGHUB_PROMPT_NODE_ID", "86")
PROMPT_FIELD = os.getenv("RUNNINGHUB_PROMPT_FIELD_NAME", "text")

# Seed node (RH_Nano_Banana_Image2Image)
SEED_NODE_ID = os.getenv("RUNNINGHUB_SEED_NODE_ID", "50")
SEED_FIELD = os.getenv("RUNNINGHUB_SEED_FIELD_NAME", "seed")

# Fixed prompt (user cannot edit)
FIXED_PROMPT = "圖中的人背著背包在身後"

# Seed max per RunningHub validation (2^31 - 1)
MAX_SEED = 2147483647

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

# Serve public/ under /static (prevents API routes getting intercepted)
app.mount("/static", StaticFiles(directory="public"), name="static")


@app.get("/")
def home():
    return FileResponse("public/index.html")


@app.get("/healthz")
def healthz():
    return "ok"


# ---------------- HELPERS ----------------

def bytes_to_dataurl(content: bytes, filename: str) -> str:
    mime, _ = mimetypes.guess_type(filename)
    mime = mime or "application/octet-stream"
    b64 = base64.b64encode(content).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def random_seed() -> int:
    # secure random seed in [0..MAX_SEED]
    return secrets.randbelow(MAX_SEED + 1)


def clamp_seed(val: int) -> int:
    if val < 0:
        return 0
    if val > MAX_SEED:
        return MAX_SEED
    return val


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
    resp = rh_post(
        "/task/openapi/create",
        {
            "apiKey": RUNNINGHUB_API_KEY,
            "workflowId": WORKFLOW_ID,
            "nodeInfoList": node_info_list,
            "addMetadata": True,
            "instanceType": "plus",
            "usePersonalQueue": "true",
        },
    )

    if resp.get("code") != 0:
        raise RuntimeError(f"Create error: {resp}")

    task_id = resp.get("data", {}).get("taskId") or resp.get("data", {}).get("id")
    if not task_id:
        raise RuntimeError(f"Create succeeded but no taskId: {resp}")
    return str(task_id)


def poll_outputs(task_id: str, timeout_sec: int = 300, interval_sec: int = 3):
    start = time.time()
    while time.time() - start < timeout_sec:
        resp = rh_post(
            "/task/openapi/outputs",
            {
                "apiKey": RUNNINGHUB_API_KEY,
                "taskId": task_id,
            },
        )

        code = resp.get("code")

        if code == 0:
            data = resp.get("data", [])
            if isinstance(data, list) and len(data) > 0:
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


# ---------------- API ----------------

@app.post("/api/generate")
async def generate(
    userImage: UploadFile = File(...),
    productOption: str = Form(...),  # "1" or "2" for now
    seed: str = Form(None),
    fixedSeed: str = Form("false"),
):
    # Validate product option (matches Switch inputs)
    try:
        opt = int(productOption)
    except:
        return JSONResponse({"error": "productOption must be a number (e.g. 1 or 2)."}, status_code=400)

    if opt not in (1, 2):
        return JSONResponse({"error": "productOption must be 1 or 2 (for now)."}, status_code=400)

    # Read uploaded file
    content = await userImage.read()
    if not content:
        return JSONResponse({"error": "Empty upload."}, status_code=400)

    user_dataurl = bytes_to_dataurl(content, userImage.filename or "user.png")

    # Seed handling (clamped to 2147483647)
    seed_val = random_seed()
    if fixedSeed == "true":
        try:
            seed_val = clamp_seed(int(seed))
        except:
            return JSONResponse({"error": "Invalid seed. Must be an integer."}, status_code=400)

    try:
        node_info_list = [
            {"nodeId": PROMPT_NODE_ID, "fieldName": PROMPT_FIELD, "fieldValue": FIXED_PROMPT},
            {"nodeId": USER_NODE_ID, "fieldName": USER_FIELD, "fieldValue": user_dataurl},
            {"nodeId": SWITCH_NODE_ID, "fieldName": SWITCH_FIELD, "fieldValue": str(opt)},
            {"nodeId": SEED_NODE_ID, "fieldName": SEED_FIELD, "fieldValue": str(seed_val)},
        ]

        task_id = create_task(node_info_list)
        outputs = poll_outputs(task_id)
        image_url = pick_image_url(outputs)

        if not image_url:
            raise RuntimeError(f"No imageUrl found in outputs: {outputs}")

        return {"imageUrl": image_url, "seed": seed_val, "taskId": task_id}

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
