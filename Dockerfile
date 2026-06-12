FROM python:3.11-slim

# 安装 ffmpeg
RUN apt-get update && apt-get install -y ffmpeg wget && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY app.py server.py index.html ./
COPY bin/ bin/

# 创建下载目录
RUN mkdir -p /data/downloads

EXPOSE 7788

CMD ["python", "server.py"]
