FROM python:3.11-slim

# 安装 ffmpeg（yt-dlp 合并音视频需要）
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/* && \
    ffmpeg -version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py server.py index.html ./

RUN mkdir -p /data/downloads

ENV FFMPEG_LOCATION=/usr/bin/ffmpeg

EXPOSE 10000

CMD ["python", "server.py"]
