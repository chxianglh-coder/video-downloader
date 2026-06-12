#!/usr/bin/env python
"""视频下载平台 - 生产级启动脚本 (waitress + 自动重启)"""
import sys, os, time, subprocess, socket
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOG_FILE = BASE_DIR / "server.log"
PORT = 7788

def is_port_open(port):
    """检查端口是否已被占用"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(('127.0.0.1', port))
    sock.close()
    return result == 0

def start_server():
    """启动 waitress 生产服务器"""
    sys.path.insert(0, str(BASE_DIR))
    from app import app

    from waitress import serve
    print(f"[启动] 视频下载平台 → http://0.0.0.0:{PORT}")
    print(f"[启动] 局域网地址 → http://{socket.gethostbyname(socket.gethostname())}:{PORT}")
    sys.stdout.flush()
    serve(app, host='0.0.0.0', port=PORT, threads=8)

if __name__ == "__main__":
    os.chdir(str(BASE_DIR))

    while True:
        try:
            start_server()
        except KeyboardInterrupt:
            print("\n[退出] 用户手动停止")
            break
        except Exception as e:
            print(f"[崩溃] {e}，3秒后自动重启...")
            time.sleep(3)
            continue
