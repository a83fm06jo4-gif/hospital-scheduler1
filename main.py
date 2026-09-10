"""
AI 智能醫院排班系統 - 後端 (v6：支援雲端資料儲存)

新增功能：
- 支援 JSONBin.io 作為雲端資料儲存（設定 JSONBIN_API_KEY / JSONBIN_BIN_ID 環境變數即啟用），
  解決 Render 等平台免費方案沒有硬碟持久化、重啟或重新部署後資料被重置的問題。
  未設定這兩個環境變數時，自動退回原本的本機 JSON 檔案儲存方式（本機開發測試用）。

其餘功能（密碼雜湊、Token 登入、資歷分級、各班人數與資歷需求、預假審核流程、
D/E/N/G 班別、班別銜接規則）維持前一版邏輯。
"""

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from ortools.sat.python import cp_model
from openpyxl import Workbook
from typing import Optional, Dict
import json
import os
import hashlib
import secrets
import time
import threading
import uuid
import requests

app = FastAPI(title="醫院排班系統 API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SHIFT_CODES = ["D", "E", "N"]
ALL_SHIFT_CODES = ["D", "E", "N", "G"]

DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(__file__))
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "data_store.json")
_lock = threading.Lock()

TOKENS = {}
TOKEN_TTL_SECONDS = 8 * 60 * 60

# ---------------------------------------------------------------------------
# 雲端資料儲存 (JSONBin.io)
# ---------------------------------------------------------------------------

JSONBIN_API_KEY = (os.environ.get("JSONBIN_API_KEY") or "").strip()
JSONBIN_BIN_ID = (os.environ.get("JSONBIN_BIN_ID") or "").strip()
USE_CLOUD_STORAGE = bool(JSONBIN_API_KEY and JSONBIN_BIN_ID)
JSONBIN_BASE = "https://api.jsonbin.io/v3/b"


def _cloud_load_raw():
    resp = requests.get(
        f"{JSONBIN_BASE}/{JSONBIN_BIN_ID}/latest",
        headers={"X-Master-Key": JSONBIN_API_KEY},
        timeout=10,
    )
    if not resp.ok:
        print(f"[cloud storage] GET 失敗，狀態碼 {resp.status_code}，回應內容：{resp.text[:500]}")
    resp.raise_for_status()
    return resp.json()["record"]


def _cloud_save_raw(data):
    resp = requests.put(
        f"{JSONBIN_BASE}/{JSONBIN_BIN_ID}",
        headers={"X-Master-Key": JSONBIN_API_KEY, "Content-Type": "application/json"},
        json=data,
        timeout=10,
    )
    if not resp.ok:
        print(f"[cloud storage] PUT 失敗，狀態碼 {resp.status_code}，回應內容：{resp.text[:500]}")
    resp.raise_for_status()

# ---------------------------------------------------------------------------
# 密碼雜湊
# ---------------------------------------------------------------------------

def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return salt.hex() + ":" + dk.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
        return secrets.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 資料持久化
# ---------------------------------------------------------------------------

ADMIN_DEFAULT_PW = (os.environ.get("ADMIN_PASSWORD") or "admin123").strip()
NURSE_DEFAULT_PW = (os.environ.get("NURSE_DEFAULT_PASSWORD") or "nurse123").strip()
DEFAULT_NUM_NURSES = 26


def _default_data():
    users = {
        "admin": {"role": "admin", "id": -1, "name": "管理員",
                  "password_hash": hash_password(ADMIN_DEFAULT_PW)},
    }
    for n in range(DEFAULT_NUM_NURSES):
        users[f"nurse{n}"] = {
            "role": "nurse", "id": n, "name": f"護理師 {n}",
            "level": 1,
            "password_hash": hash_password(NURSE_DEFAULT_PW),
        }
    return {
        "config": {
            "num_nurses": DEFAULT_NUM_NURSES,
            "num_days": 7,
            "max_consecutive_days": 6,
            # 每班每天最少需要幾人（管理員可調整）
            "shift_min_count": {"D": 1, "E": 1, "N": 1},
            # 每班每天至少需要幾位「資歷 >= min_level」的護理師；min_count 為 0 代表不設限制
            "level_requirements": {
                "D": {"min_level": 0, "min_count": 0},
                "E": {"min_level": 0, "min_count": 0},
                "N": {"min_level": 0, "min_count": 0},
            },
        },
        "users": users,
        "day_off_db": [],  # [{"id","nurse_id","day","status":"pending"|"approved"|"rejected"}]
        "schedule": {},
    }


def _apply_migrations(data):
    """向下相容：補齊舊資料檔/舊雲端資料裡缺少的新欄位"""
    cfg = data.setdefault("config", {})
    cfg.setdefault("shift_min_count", {"D": 1, "E": 1, "N": 1})
    cfg.setdefault("level_requirements", {
        "D": {"min_level": 0, "min_count": 0},
        "E": {"min_level": 0, "min_count": 0},
        "N": {"min_level": 0, "min_count": 0},
    })
    for u in data.get("users", {}).values():
        if u.get("role") == "nurse":
            u.setdefault("level", 1)
    for entry in data.get("day_off_db", []):
        entry.setdefault("id", uuid.uuid4().hex)
        entry.setdefault("status", "approved")
    return data


def load_data():
    if USE_CLOUD_STORAGE:
        try:
            data = _cloud_load_raw()
            if not isinstance(data, dict) or "config" not in data:
                raise ValueError("雲端 Bin 目前是空的或格式不符，視為初次使用，將寫入預設資料")
            return _apply_migrations(data)
        except Exception as e:
            print(f"[cloud storage] 讀取雲端資料失敗，改用預設資料：{e}")
            data = _default_data()
            try:
                _cloud_save_raw(data)
                print("[cloud storage] 已將預設資料寫入雲端 Bin")
            except Exception as e2:
                print(f"[cloud storage] 寫入雲端資料失敗，本次僅使用暫時的記憶體資料，尚未真正持久化：{e2}")
            return data

    if not os.path.exists(DATA_FILE):
        data = _default_data()
        save_data(data)
        return data
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return _apply_migrations(data)


def save_data(data):
    if USE_CLOUD_STORAGE:
        try:
            _cloud_save_raw(data)
        except Exception as e:
            print(f"[cloud storage] 儲存資料到雲端失敗：{e}")
            raise
        return
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_data():
    with _lock:
        return load_data()


def set_data(data):
    with _lock:
        save_data(data)


# ---------------------------------------------------------------------------
# 認證依賴
# ---------------------------------------------------------------------------

def get_current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "請先登入 (缺少 Token)")
    token = authorization.split(" ", 1)[1]
    entry = TOKENS.get(token)
    if not entry or entry["expires"] < time.time():
        TOKENS.pop(token, None)
        raise HTTPException(401, "登入已過期，請重新登入")
    data = get_data()
    user = data["users"].get(entry["username"])
    if not user:
        raise HTTPException(401, "帳號不存在")
    return {"username": entry["username"], **user}


def require_admin(user=Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "僅限管理員操作")
    return user


# ---------------------------------------------------------------------------
# Pydantic 模型
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class CreateNurseRequest(BaseModel):
    username: str
    name: str
    password: str
    level: Optional[int] = 1


class SetNurseLevelRequest(BaseModel):
    username: str
    level: int


class DayOffRequest(BaseModel):
    day: int


class DayOffReviewRequest(BaseModel):
    request_id: str
    action: str  # "approve" or "reject"


class LevelRequirement(BaseModel):
    min_level: int = 0
    min_count: int = 0


class ConfigRequest(BaseModel):
    num_nurses: int
    num_days: int
    max_consecutive_days: Optional[int] = 6
    shift_min_count: Optional[Dict[str, int]] = None
    level_requirements: Optional[Dict[str, LevelRequirement]] = None


class SaveScheduleRequest(BaseModel):
    schedule: dict


# ---------------------------------------------------------------------------
# 登入 / 登出 / 帳號管理
# ---------------------------------------------------------------------------

@app.post("/login")
def login(req: LoginRequest):
    data = get_data()
    user = data["users"].get(req.username)
    if not user or not verify_password(req.password, user.get("password_hash", "")):
        raise HTTPException(401, "帳號或密碼錯誤")
    token = secrets.token_urlsafe(32)
    TOKENS[token] = {"username": req.username, "expires": time.time() + TOKEN_TTL_SECONDS}
    return {
        "token": token, "role": user["role"], "id": user["id"],
        "name": user.get("name", req.username), "level": user.get("level"),
    }


@app.post("/logout")
def logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        TOKENS.pop(authorization.split(" ", 1)[1], None)
    return {"message": "已登出"}


@app.post("/change_password")
def change_password(req: ChangePasswordRequest, user=Depends(get_current_user)):
    data = get_data()
    record = data["users"][user["username"]]
    if not verify_password(req.old_password, record["password_hash"]):
        raise HTTPException(403, "原密碼錯誤")
    if len(req.new_password) < 6:
        raise HTTPException(400, "新密碼至少需要 6 碼")
    record["password_hash"] = hash_password(req.new_password)
    set_data(data)
    return {"message": "密碼已更新"}


@app.post("/admin/create_nurse")
def create_nurse(req: CreateNurseRequest, admin=Depends(require_admin)):
    data = get_data()
    if req.username in data["users"]:
        raise HTTPException(400, "帳號已存在")
    existing_ids = [u["id"] for u in data["users"].values() if u["role"] == "nurse"]
    new_id = max(existing_ids, default=-1) + 1
    data["users"][req.username] = {
        "role": "nurse", "id": new_id, "name": req.name,
        "level": req.level or 1,
        "password_hash": hash_password(req.password),
    }
    data["config"]["num_nurses"] = max(data["config"]["num_nurses"], new_id + 1)
    set_data(data)
    return {"message": f"已新增護理師帳號 {req.username}", "id": new_id}


@app.post("/admin/set_nurse_level")
def set_nurse_level(req: SetNurseLevelRequest, admin=Depends(require_admin)):
    data = get_data()
    user = data["users"].get(req.username)
    if not user or user["role"] != "nurse":
        raise HTTPException(404, "找不到該護理師帳號")
    user["level"] = req.level
    set_data(data)
    return {"message": f"已將 {req.username} 的資歷等級設為 {req.level}"}


@app.get("/config")
def get_config(user=Depends(get_current_user)):
    return get_data()["config"]


@app.post("/config")
def set_config(req: ConfigRequest, admin=Depends(require_admin)):
    data = get_data()
    cfg = data["config"]
    cfg["num_nurses"] = req.num_nurses
    cfg["num_days"] = req.num_days
    if req.max_consecutive_days is not None:
        cfg["max_consecutive_days"] = req.max_consecutive_days
    if req.shift_min_count is not None:
        for code in SHIFT_CODES:
            if code in req.shift_min_count:
                cfg["shift_min_count"][code] = max(0, req.shift_min_count[code])
    if req.level_requirements is not None:
        for code in SHIFT_CODES:
            if code in req.level_requirements:
                lr = req.level_requirements[code]
                cfg["level_requirements"][code] = {"min_level": lr.min_level, "min_count": lr.min_count}
    set_data(data)
    return {"message": "設定已更新", "config": cfg}


@app.get("/nurses")
def list_nurses(user=Depends(get_current_user)):
    data = get_data()
    return [
        {"id": u["id"], "name": u.get("name"), "level": u.get("level", 1)}
        for u in data["users"].values() if u["role"] == "nurse"
    ]


# ---------------------------------------------------------------------------
# 預假（含審核流程）
# ---------------------------------------------------------------------------

@app.post("/request_dayoff")
def request_dayoff(req: DayOffRequest, user=Depends(get_current_user)):
    if user["role"] != "nurse":
        raise HTTPException(403, "僅護理師可申請預假")
    data = get_data()
    if any(e["nurse_id"] == user["id"] and e["day"] == req.day and e["status"] != "rejected"
           for e in data["day_off_db"]):
        return {"message": "已經申請過此天預假（待審核或已核准中）"}
    data["day_off_db"].append({
        "id": uuid.uuid4().hex, "nurse_id": user["id"], "day": req.day, "status": "pending",
    })
    set_data(data)
    return {"message": f"已送出 Day {req.day} 預假申請，等待管理員審核"}


@app.get("/dayoffs")
def get_dayoffs(admin=Depends(require_admin)):
    """管理員檢視全部護理師的預假申請（含待審核／已核准／已駁回）"""
    data = get_data()
    users_by_id = {u["id"]: u.get("name") for u in data["users"].values() if u["role"] == "nurse"}
    result = []
    for e in data["day_off_db"]:
        result.append({**e, "nurse_name": users_by_id.get(e["nurse_id"], f"護理師 {e['nurse_id']}")})
    return result


@app.get("/dayoffs/mine")
def get_my_dayoffs(user=Depends(get_current_user)):
    data = get_data()
    return [e for e in data["day_off_db"] if e["nurse_id"] == user["id"]]


@app.post("/dayoff/review")
def review_dayoff(req: DayOffReviewRequest, admin=Depends(require_admin)):
    if req.action not in ("approve", "reject"):
        raise HTTPException(400, "action 必須是 approve 或 reject")
    data = get_data()
    entry = next((e for e in data["day_off_db"] if e["id"] == req.request_id), None)
    if not entry:
        raise HTTPException(404, "找不到該筆預假申請")
    entry["status"] = "approved" if req.action == "approve" else "rejected"
    set_data(data)
    return {"message": f"已{'核准' if req.action == 'approve' else '駁回'}該筆預假申請"}


@app.post("/dayoff/cancel")
def cancel_dayoff(req: DayOffRequest, user=Depends(get_current_user)):
    """護理師本人可撤回自己申請的預假（不論狀態）"""
    data = get_data()
    before = len(data["day_off_db"])
    data["day_off_db"] = [
        e for e in data["day_off_db"]
        if not (e["nurse_id"] == user["id"] and e["day"] == req.day)
    ]
    set_data(data)
    changed = before != len(data["day_off_db"])
    return {"message": "已撤回預假申請" if changed else "找不到該筆預假紀錄"}


# ---------------------------------------------------------------------------
# 自動排班
# ---------------------------------------------------------------------------

@app.post("/schedule")
def generate_schedule(user=Depends(get_current_user)):
    data = get_data()
    cfg = data["config"]
    num_nurses, num_days = cfg["num_nurses"], cfg["num_days"]
    max_consec = cfg.get("max_consecutive_days", 6)
    shift_min_count = cfg.get("shift_min_count", {"D": 1, "E": 1, "N": 1})
    level_requirements = cfg.get("level_requirements", {})

    nurse_levels = {}
    for u in data["users"].values():
        if u["role"] == "nurse" and u["id"] < num_nurses:
            nurse_levels[u["id"]] = u.get("level", 1)
    for n in range(num_nurses):
        nurse_levels.setdefault(n, 1)

    model = cp_model.CpModel()
    shifts = {}
    for n in range(num_nurses):
        for d in range(num_days):
            for code in SHIFT_CODES:
                shifts[(n, d, code)] = model.NewBoolVar(f"s_{n}_{d}_{code}")

    works = {}
    for n in range(num_nurses):
        for d in range(num_days):
            works[(n, d)] = model.NewBoolVar(f"w_{n}_{d}")
            daily_sum = sum(shifts[(n, d, code)] for code in SHIFT_CODES)
            model.Add(daily_sum == works[(n, d)])
            model.Add(daily_sum <= 1)

    # 每班每天至少需要 shift_min_count 指定的人數
    for d in range(num_days):
        for code in SHIFT_CODES:
            min_count = shift_min_count.get(code, 1)
            model.Add(sum(shifts[(n, d, code)] for n in range(num_nurses)) >= min_count)

    # 資歷分級限制：每班每天至少需要 min_count 位 level >= min_level 的護理師
    for code in SHIFT_CODES:
        lr = level_requirements.get(code, {"min_level": 0, "min_count": 0})
        min_level, min_count = lr.get("min_level", 0), lr.get("min_count", 0)
        if min_count and min_count > 0:
            senior_nurses = [n for n in range(num_nurses) if nurse_levels.get(n, 1) >= min_level]
            for d in range(num_days):
                model.Add(sum(shifts[(n, d, code)] for n in senior_nurses) >= min_count)

    # 只避開「已核准」的預假天數
    for entry in data["day_off_db"]:
        if entry.get("status") != "approved":
            continue
        n, d = entry["nurse_id"], entry["day"]
        if n < num_nurses and d < num_days:
            model.Add(sum(shifts[(n, d, code)] for code in SHIFT_CODES) == 0)

    # 班別銜接規則：不可 E接D、不可 D或E接N、不可 N接D或E
    for n in range(num_nurses):
        for d in range(num_days - 1):
            model.Add(shifts[(n, d, "E")] + shifts[(n, d + 1, "D")] <= 1)
            model.Add(shifts[(n, d, "D")] + shifts[(n, d + 1, "N")] <= 1)
            model.Add(shifts[(n, d, "E")] + shifts[(n, d + 1, "N")] <= 1)
            model.Add(shifts[(n, d, "N")] + shifts[(n, d + 1, "D")] <= 1)
            model.Add(shifts[(n, d, "N")] + shifts[(n, d + 1, "E")] <= 1)

    # 連續上班天數上限
    if max_consec and max_consec > 0:
        window = max_consec + 1
        for n in range(num_nurses):
            for start in range(0, num_days - window + 1):
                model.Add(sum(works[(n, d)] for d in range(start, start + window)) <= max_consec)

    # 公平性：讓每位護理師的總上班天數盡量平均
    total_shifts_per_nurse = [
        sum(shifts[(n, d, code)] for d in range(num_days) for code in SHIFT_CODES)
        for n in range(num_nurses)
    ]
    max_load = model.NewIntVar(0, num_days, "max_load")
    min_load = model.NewIntVar(0, num_days, "min_load")
    model.AddMaxEquality(max_load, total_shifts_per_nurse)
    model.AddMinEquality(min_load, total_shifts_per_nurse)
    model.Minimize(max_load - min_load)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 15.0
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise HTTPException(
            400,
            "目前條件無解，請檢查：已核准預假數量是否過多、各班最少人數與資歷需求是否設定過高、"
            "或連續上班天數限制是否過於嚴格。"
        )

    result = {
        str(d): [
            {"nurse": n, "shift": code}
            for n in range(num_nurses)
            for code in SHIFT_CODES
            if solver.Value(shifts[(n, d, code)])
        ]
        for d in range(num_days)
    }

    data["schedule"] = result
    set_data(data)
    return result


@app.get("/schedule")
def get_schedule(user=Depends(get_current_user)):
    return get_data()["schedule"]


@app.post("/schedule/save")
def save_schedule(req: SaveScheduleRequest, user=Depends(get_current_user)):
    if user["role"] != "admin":
        for day, entries in req.schedule.items():
            for e in entries:
                if e.get("shift") == "G":
                    raise HTTPException(403, "僅管理員可指派 G 班（12 小時班）")
    for day, entries in req.schedule.items():
        for e in entries:
            if e.get("shift") not in ALL_SHIFT_CODES:
                raise HTTPException(400, f"無效的班別代碼：{e.get('shift')}")
    data = get_data()
    data["schedule"] = req.schedule
    set_data(data)
    return {"message": "班表已儲存"}


# ---------------------------------------------------------------------------
# Excel 匯出
# ---------------------------------------------------------------------------

SHIFT_LABELS = {"D": "白班", "E": "小夜班", "N": "大夜班", "G": "12小時班"}


@app.get("/export_excel")
def export_excel(user=Depends(get_current_user)):
    data = get_data()
    schedule = data["schedule"]
    users_by_id = {u["id"]: u.get("name", f"護理師 {u['id']}")
                   for u in data["users"].values() if u["role"] == "nurse"}

    wb = Workbook()
    ws = wb.active
    ws.title = "醫院班表"
    ws.append(["日期", "護理師 ID", "護理師姓名", "班別代碼", "班別名稱"])
    for day in sorted(schedule.keys(), key=lambda x: int(x)):
        for e in schedule[day]:
            code = e["shift"]
            ws.append([
                f"Day {day}", e["nurse"], users_by_id.get(e["nurse"], e["nurse"]),
                code, SHIFT_LABELS.get(code, "")
            ])
    path = os.path.join(DATA_DIR, "schedule_export.xlsx")
    wb.save(path)
    return {"message": "匯出成功", "file": os.path.basename(path)}


# ---------------------------------------------------------------------------
# 前端頁面
# ---------------------------------------------------------------------------

_FRONTEND_PATH = os.path.join(os.path.dirname(__file__), "index.html")


@app.get("/")
def root():
    if os.path.exists(_FRONTEND_PATH):
        return FileResponse(_FRONTEND_PATH)
    return {"message": "醫院排班系統 API 運作中，請開啟 index.html"}
