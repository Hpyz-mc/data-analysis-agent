FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app

# The chart tool needs a Chinese-capable font in the Linux container.
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources \
    && sed -i 's/security.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install --no-install-recommends -y fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY view_session_history.py /app/view_session_history.py

RUN groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid agent --home-dir /app --no-create-home agent \
    && mkdir -p /app/outputs/charts /tmp/matplotlib \
    && chown -R agent:agent /app/outputs /tmp/matplotlib

USER agent
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
