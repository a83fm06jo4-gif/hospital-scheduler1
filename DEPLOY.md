# 部署說明

## 一、本機直接執行（不用 Docker）

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

啟動後瀏覽器打開 `http://127.0.0.1:8000` 即可看到系統（後端已一併提供前端頁面）。
也可以雙擊 `index.html` 直接開啟，前端會自動連到 `http://127.0.0.1:8000` 的 API。

首次啟動會自動建立預設帳號：

| 帳號 | 密碼 | 角色 |
|---|---|---|
| admin | admin123（或環境變數 ADMIN_PASSWORD） | 管理員 |
| nurse0 ~ nurse2 | nurse123（或環境變數 NURSE_DEFAULT_PASSWORD） | 護理師 |

**強烈建議登入後立即在「修改我的密碼」區塊更改預設密碼。**

---

## 二、用 Docker 執行

### 方法 A：docker-compose（推薦，含資料持久化）

```bash
docker compose up -d --build
```

打開 `http://localhost:8000` 即可使用。資料會存在 named volume `scheduler_data` 裡，重建容器不會遺失。

**部署前請先修改 `docker-compose.yml` 裡的密碼：**

```yaml
environment:
  - ADMIN_PASSWORD=你的管理員密碼
  - NURSE_DEFAULT_PASSWORD=你的護理師預設密碼
```

停止服務：

```bash
docker compose down
```

### 方法 B：純 Docker 指令

```bash
docker build -t hospital-scheduler .
docker run -d -p 8000:8000 \
  -e ADMIN_PASSWORD=你的管理員密碼 \
  -e NURSE_DEFAULT_PASSWORD=你的護理師預設密碼 \
  -v scheduler_data:/app/data \
  -e DATA_DIR=/app/data \
  --name hospital-scheduler \
  hospital-scheduler
```

---

## 三、部署到雲端

### Render

1. 把這個資料夾推到 GitHub repo。
2. Render 控制台 → **New → Web Service**，選擇該 repo。
3. Render 會偵測到 `Dockerfile` 自動用它建置（不需要額外設定 Build/Start Command）。
   - 若 Render 沒自動偵測，Start Command 填：`uvicorn main:app --host 0.0.0.0 --port $PORT`
     （此時 Dockerfile 內寫死 8000 埠，改用 Render 提供的 `$PORT` 較保險，可將 Dockerfile 的 `CMD` 改成
     `CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}`，並用 shell form 而非 exec form）
4. Environment 分頁新增環境變數：
   - `ADMIN_PASSWORD`
   - `NURSE_DEFAULT_PASSWORD`
   - `DATA_DIR` = `/app/data`（若要資料持久化，需另外掛 Render 的 **Persistent Disk** 到 `/app/data`；Render 免費方案不含持久化磁碟，重新部署資料會重置，請留意）
5. 部署完成後 Render 會給一個 `https://xxx.onrender.com` 網址，直接打開即可使用。

### Railway

1. 一樣先推到 GitHub。
2. Railway 控制台 → **New Project → Deploy from GitHub repo**。
3. Railway 會自動偵測 `Dockerfile` 建置。
4. **Variables** 分頁新增：
   - `ADMIN_PASSWORD`
   - `NURSE_DEFAULT_PASSWORD`
   - `DATA_DIR=/app/data`
5. 若要資料持久化，於 Railway 專案加一個 **Volume**，掛載路徑設為 `/app/data`。
6. **Settings → Networking** 產生一個公開網域，即可對外存取。

### 共通注意事項

- 系統目前用**單一伺服器行程**保存登入 Token（存在記憶體），若雲端平台會自動重啟/多實例擴展（scale > 1 instance），使用者可能需要重新登入，或不同請求打到不同實例導致 Token 失效。單一 instance、小型單位使用沒問題；若要多實例，需要把 Token 改存到 Redis 之類的共用儲存（目前版本未包含，之後可以再加）。
- `CORSMiddleware` 目前開放所有來源（`allow_origins=["*"]`），部署到正式網域後建議改成只允許你的網域，例如：
  ```python
  app.add_middleware(
      CORSMiddleware,
      allow_origins=["https://your-domain.com"],
      allow_methods=["*"],
      allow_headers=["*"],
  )
  ```
- 務必透過環境變數設定 `ADMIN_PASSWORD` / `NURSE_DEFAULT_PASSWORD`，不要用預設密碼上線。
- 目前用 HTTP（無 HTTPS）僅適合本機測試；正式對外服務請確保雲端平台有提供 HTTPS（Render / Railway 預設網域都有）。
