FROM python:3.12.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /workspace

# 依赖层独立缓存；构建期间不读取实际环境配置。
COPY requirement.txt ./
RUN python -m pip install --no-cache-dir -r requirement.txt \
    && python -m pip check

# 命名卷首次挂载继承目录权限；真实头像和附件由运行时写入。
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir -p /workspace/data/user/avatar /workspace/data/user/upload-file \
    && chown -R app:app /workspace/data

COPY --chown=app:app app ./app
COPY --chown=app:app assets/tool-tag ./assets/tool-tag

USER app
EXPOSE 8000

# 只检查 Web 入口，不触发登录、全表写入或模型调用。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8000/openapi.json', timeout=3).close()"]

# 任务、SSE 和平台会话驻留于进程内，固定单 worker。
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
