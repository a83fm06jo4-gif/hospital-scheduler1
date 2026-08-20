FROM python:3.11-slim

WORKDIR /app

# 先複製 requirements 以善用 Docker layer cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製其餘程式碼（index.html 一併打包，讓容器可同時提供前端頁面）
COPY main.py .
COPY index.html .

# 資料檔會寫在 /app/data_store.json，建議掛 volume 保留
EXPOSE 8000

# 建議正式環境設定 ADMIN_PASSWORD / NURSE_DEFAULT_PASSWORD 環境變數
# 使用 shell form 以便讀取雲端平台 (Render/Railway) 常見的 $PORT 環境變數，本機沒設定時預設 8000
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
