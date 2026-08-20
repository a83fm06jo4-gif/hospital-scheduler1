@echo off
pip install -r requirements.txt
echo 套件檢查完成，正在啟動後端...
echo 啟動後請直接用瀏覽器開啟 index.html
uvicorn main:app --reload
pause
