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
| nurse0 ~ nurse25 | nurse123（或環境變數 NURSE_DEFAULT_PASSWORD） | 護理師（共 26 位） |

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
   - 若要用雲端資料庫解決資料不持久的問題，請參考下方「四、設定雲端資料庫」再加上
     `JSONBIN_API_KEY` / `JSONBIN_BIN_ID`
5. 部署完成後 Render 會給一個 `https://xxx.onrender.com` 網址，直接打開即可使用。

### Railway

1. 一樣先推到 GitHub。
2. Railway 控制台 → **New Project → Deploy from GitHub repo**。
3. Railway 會自動偵測 `Dockerfile` 建置。
4. **Variables** 分頁新增：
   - `ADMIN_PASSWORD`
   - `NURSE_DEFAULT_PASSWORD`
   - 同上，建議加上 `JSONBIN_API_KEY` / `JSONBIN_BIN_ID`（見下方第四節）
5. **Settings → Networking** 產生一個公開網域，即可對外存取。

### 共通注意事項

- 系統目前用**單一伺服器行程**保存登入 Token（存在記憶體），若雲端平台會自動重啟/多實例擴展（scale > 1 instance），使用者可能需要重新登入。單一 instance、小型單位使用沒問題。
- `CORSMiddleware` 目前開放所有來源（`allow_origins=["*"]`），部署到正式網域後建議改成只允許你的網域。
- 務必透過環境變數設定 `ADMIN_PASSWORD` / `NURSE_DEFAULT_PASSWORD`，不要用預設密碼上線。

---

## 四、設定雲端資料庫（解決資料不持久的問題）

免費方案（Render / Railway 免費層）預設沒有硬碟持久化，伺服器重啟或重新部署時，`data_store.json`
裡的帳號密碼、班表、預假紀錄都會被重置。設定好以下兩個環境變數後，系統會自動改用
[JSONBin.io](https://jsonbin.io)（免費雲端資料儲存）取代本機檔案，資料就能永久保存。

### 步驟

1. 到 https://jsonbin.io 免費註冊一個帳號（email 或 Google 登入都可以）
2. 登入後，左側選單找 **API Keys**，複製你的 **X-Master-Key**（一長串英數字）
3. 回到主畫面，點 **Create Bin**（建立一個新的資料倉庫），內容隨便貼 `{}` 就好，按 Create
4. 建立完成後，網址列或 Bin 詳細資訊裡會有一串 **Bin ID**（例如 `65f1a2b3c4d5e6f7g8h9i0j1`），複製起來
5. 到 Render（或 Railway）的 **Environment** 分頁，新增兩個環境變數：
   - `JSONBIN_API_KEY` = 剛剛複製的 X-Master-Key
   - `JSONBIN_BIN_ID` = 剛剛複製的 Bin ID
6. 存檔後 Render 會自動重新部署，之後系統就會把所有資料讀寫到這個雲端 Bin，不再依賴本機檔案

### 注意事項

- 這兩個環境變數**沒有設定時**，系統會自動退回本機檔案儲存（本機測試不受影響，不用擔心）
- JSONBin.io 免費方案有請求次數限制（通常每月數萬次，一般小型單位使用綽綽有餘）
- `X-Master-Key` 等同於資料庫密碼，不要外流或分享給不信任的人
- 設定完成後，可以到 https://jsonbin.io 網站上你剛建立的那個 Bin，重新整理頁面應該就能看到
  系統寫入的完整資料（帳號、班表等），確認雲端儲存有正常運作
