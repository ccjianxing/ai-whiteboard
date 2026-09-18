# AI 白板 —— 纯 Python 标准库，无需 pip install
# 作者：ccjianxing ｜ https://github.com/ccjianxing/ai-whiteboard ｜ MIT License
FROM python:3.11-slim

WORKDIR /app
COPY . /app

ENV PORT=9091
ENV PYTHONUNBUFFERED=1
EXPOSE 9091

# 注意：容器里没有 Edge/Chrome，「网页截图」功能不可用（会明确报错，不影响其它功能）
CMD ["python", "server_v2.py"]
