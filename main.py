"""
AI 智能醫院排班系統 - 後端 (v3：D/E/N 班別命名 + 新排班規則)

班別代碼：
- D = 白班
- E = 小夜班
- N = 大夜班
- OFF = 休假（前端顯示用，後端資料上代表「當天沒有排班」）

排班規則：
- 連續上班天數上限 (預設最多連續 6 天，第 7 天強制休)
- 不可 E 接 D（前一天上 E，隔天不可上 D）
- 不可 D 接 N、不可 E 接 N（前一天上 D 或 E，隔天不可上 N）
- 每班每天至少 1 人
- 已核准的預假天數不可排班
- 盡量平均每位護理師的總上班天數（公平性）

其餘功能（密碼雜湊、Token 登入、資料持久化）與前一版相同。
"""

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from ortools.sat.python import cp_model
from openpyxl import Workbook
from typing import Optional
import json
import os
import hashlib
import secrets
import time
import threading

app = FastAPI(title="醫院排班系統 API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 班別代碼固定為 D / E / N 三種，OFF 代表當天沒有任何班別（不佔資料，純顯示用）
SHIFT_CODES = ["D", "E", "N"]

# 資料存放目錄可透過 DATA_DIR 環境變數覆寫（Docker 中掛 volume 用）
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(__file__))
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "data_store.json")
_lock = threading.Lock()

TOKENS = {}  # token -> {"username": str, "expires": float}
TOKEN_TTL_SECONDS = 8 * 60 * 60  # 8 小時

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

ADMIN_DEFAULT_PW = os.environ.get("ADMIN_PASSWORD", "admin123")
NURSE_DEFAULT_PW = os.environ.get("NURSE_DEFAULT_PASSWORD", "nurse123")
DEFAULT_NUM_NURSES = 26


def _default_data():
    users = {
        "admin": {"role": "admin", "id": -1, "name": "管理員",
                  "password_hash": hash_password(ADMIN_DEFAULT_PW)},
    }
    for n in range(DEFAULT_NUM_NURSES):
        users[f"nurse{n}"] = {
            "role": "nurse", "id": n, "name": f"護理師 {n}",
            "password_hash": hash_password(NURSE_DEFAULT_PW),
        }
    return {
        "config": {
            "num_nurses": DEFAULT_NUM_NURSES,
            "num_days": 7,
            "max_consecutive_days": 6,
        },
        "users": users,
        "day_off_db": [],
        "schedule": {},
    }


def load_data():
    if not os.path.exists(DATA_FILE):
        data = _default_data()
        save_data(data)
        return data
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data):
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


class DayOffRequest(BaseModel):
    day: int


class ConfigRequest(BaseModel):
    num_nurses: int
    num_days: int
    max_consecutive_days: Optional[int] = 6


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
    return {"token": token, "role": user["role"], "id": user["id"], "name": user.get("name", req.username)}


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
        "password_hash": hash_password(req.password),
    }
    data["config"]["num_nurses"] = max(data["config"]["num_nurses"], new_id + 1)
    set_data(data)
    return {"message": f"已新增護理師帳號 {req.username}", "id": new_id}


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
    set_data(data)
    return {"message": "設定已更新", "config": cfg}


@app.get("/nurses")
def list_nurses(user=Depends(get_current_user)):
    data = get_data()
    return [{"id": u["id"], "name": u.get("name")} for u in data["users"].values() if u["role"] == "nurse"]


# ---------------------------------------------------------------------------
# 預假
# ---------------------------------------------------------------------------

@app.post("/request_dayoff")
def request_dayoff(req: DayOffRequest, user=Depends(get_current_user)):
    if user["role"] != "nurse":
        raise HTTPException(403, "僅護理師可申請預假")
    data = get_data()
    if any(e["nurse_id"] == user["id"] and e["day"] == req.day for e in data["day_off_db"]):
        return {"message": "已經申請過此天預假"}
    data["day_off_db"].append({"nurse_id": user["id"], "day": req.day})
    set_data(data)
    return {"message": f"已申請 Day {req.day} 預假"}


@app.get("/dayoffs")
def get_dayoffs(user=Depends(get_current_user)):
    return get_data()["day_off_db"]


@app.post("/dayoff/cancel")
def cancel_dayoff(req: DayOffRequest, user=Depends(get_current_user)):
    data = get_data()
    before = len(data["day_off_db"])
    data["day_off_db"] = [
        e for e in data["day_off_db"]
        if not (e["nurse_id"] == user["id"] and e["day"] == req.day)
    ]
    set_data(data)
    changed = before != len(data["day_off_db"])
    return {"message": "已取消預假" if changed else "找不到該筆預假紀錄"}


# ---------------------------------------------------------------------------
# 自動排班
# ---------------------------------------------------------------------------

@app.post("/schedule")
def generate_schedule(user=Depends(get_current_user)):
    data = get_data()
    cfg = data["config"]
    num_nurses, num_days = cfg["num_nurses"], cfg["num_days"]
    max_consec = cfg.get("max_consecutive_days", 6)

    model = cp_model.CpModel()
    # shifts[(n, d, code)] = True 代表護理師 n 在第 d 天上 code 班 (D/E/N)
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
            model.Add(daily_sum <= 1)  # 每人每天最多一班

    # 每班每天至少 1 人
    for d in range(num_days):
        for code in SHIFT_CODES:
            model.Add(sum(shifts[(n, d, code)] for n in range(num_nurses)) >= 1)

    # 已核准的預假天數不可排班
    for entry in data["day_off_db"]:
        n, d = entry["nurse_id"], entry["day"]
        if n < num_nurses and d < num_days:
            model.Add(sum(shifts[(n, d, code)] for code in SHIFT_CODES) == 0)

    # 班別銜接規則：不可 E接D、不可 D接N、不可 E接N
    for n in range(num_nurses):
        for d in range(num_days - 1):
            # 前一天 E，隔天不可 D
            model.Add(shifts[(n, d, "E")] + shifts[(n, d + 1, "D")] <= 1)
            # 前一天 D，隔天不可 N
            model.Add(shifts[(n, d, "D")] + shifts[(n, d + 1, "N")] <= 1)
            # 前一天 E，隔天不可 N
            model.Add(shifts[(n, d, "E")] + shifts[(n, d + 1, "N")] <= 1)

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
        raise HTTPException(400, "目前條件無解，請檢查預假數量或放寬限制（例如連續上班天數）")

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
    data = get_data()
    data["schedule"] = req.schedule
    set_data(data)
    return {"message": "班表已儲存"}


# ---------------------------------------------------------------------------
# Excel 匯出
# ---------------------------------------------------------------------------

SHIFT_LABELS = {"D": "白班", "E": "小夜班", "N": "大夜班"}


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
