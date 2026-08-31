# B1: 让 docker compose up 可构建（Python 3.12，兼容 PEP 604 语法）
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# P0 可重建（2026-08-31）：fresh build 曾因网络慢（索引页 15s+）pip 超时失败——
# 非依赖冲突（清华源含全部 aarch64 wheel）。加大 timeout + 重试应对慢网。
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
COPY requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} --timeout 600 --retries 5 -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
