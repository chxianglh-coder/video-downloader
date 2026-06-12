"""
B站/抖音 视频下载平台 - Flask 后端
支持：视频信息获取、MP4/MP3/原画下载、实时进度推送、下载记录
抖音使用移动端 API (aweme.snssdk.com) 绕开 JS 反爬
"""

import os
import sys
import json
import uuid
import threading
import subprocess
import re
import time
from pathlib import Path
from typing import Optional
from flask import Flask, request, jsonify, send_file, Response, send_from_directory
from flask_cors import CORS
import urllib.request
import ssl


# ── 抖音移动端 API ──────────────────────────────────────────
# 绕开 www.douyin.com 的 JSVM 反爬，直接使用移动端 APP API

_douyin_session = None

def _get_douyin_session():
    """获取带 Cookie 的 requests Session，用于抖音移动端 API"""
    global _douyin_session
    if _douyin_session is not None:
        return _douyin_session
    
    try:
        import requests
    except ImportError:
        return None
    
    _douyin_session = requests.Session()
    _douyin_session.headers.update({
        'User-Agent': 'com.ss.android.ugc.aweme/350103 (Linux; U; Android 14; zh_CN; SM-S9080; Build/UP1A.231005.007; Cronet/TTNetVersion:591a2435 2025-09-17 QuicVersion:ee3bd53c 2025-07-23)',
        'Accept': 'application/json',
    })
    
    # 加载 Cookie 文件
    cfile = _find_cookie_file("douyin.com")
    if cfile:
        cookies = {}
        with open(cfile) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) >= 7:
                    name, value = parts[5], parts[6]
                    if name and value:
                        cookies[name] = value
        _douyin_session.cookies.update(cookies)
    return _douyin_session


def _fetch_douyin_mobile(video_id: str) -> Optional[dict]:
    """使用移动端 API 获取抖音视频信息，返回 {title, thumbnail, duration, ...}"""
    session = _get_douyin_session()
    if not session:
        return None
    
    try:
        r = session.get(
            'https://aweme.snssdk.com/aweme/v1/aweme/detail/',
            params={
                'aweme_id': video_id,
                'aid': '1128',
                'app_name': 'aweme',
                'version_code': '350103',
                'version_name': '35.1.0',
                'device_platform': 'android',
                'os': 'android',
            },
            timeout=15,
        )
        if r.status_code != 200 or len(r.text) < 100:
            return None
        
        data = r.json()
        aweme = data.get('aweme_detail', {})
        if not aweme:
            return None
        
        video = aweme.get('video', {})
        play_addr = video.get('play_addr', {})
        download_addr = video.get('download_addr', {})
        
        # 选择最高清晰度
        bit_rate_list = video.get('bit_rate', [])
        best_video = None
        if bit_rate_list:
            best = max(bit_rate_list, key=lambda x: x.get('bit_rate', 0))
            best_play = best.get('play_addr', {})
            best_video = best_play.get('url_list', [None])[0] if best_play else None
        
        # 无水印下载地址
        no_watermark = None
        if download_addr:
            no_watermark = download_addr.get('url_list', [None])[0]
        
        duration_ms = aweme.get('duration', 0)
        minutes, seconds = divmod(int(duration_ms / 1000), 60)
        
        return {
            'title': aweme.get('desc', '') or aweme.get('preview_title', '') or '抖音视频',
            'thumbnail': (video.get('cover', {}) or {}).get('url_list', [''])[0],
            'uploader': aweme.get('author', {}).get('nickname', ''),
            'duration': f"{minutes:02d}:{seconds:02d}" if duration_ms else "--",
            'view_count': aweme.get('statistics', {}).get('play_count', 0),
            'like_count': aweme.get('statistics', {}).get('digg_count', 0),
            'extractor': 'douyin-mobile',
            'download_url': best_video or (play_addr.get('url_list', [None])[0]),
            'download_url_nwm': no_watermark,
            'video_id': video_id,
        }
    except Exception as e:
        print(f"[Douyin Mobile API Error] {e}")
        return None

# ── 短链解析 ────────────────────────────────────────────────

# 允许忽略 SSL 证书错误（某些 CDN 有证书问题）
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

def resolve_short_link(url: str) -> str:
    """解析抖音/B站 短链，跟随重定向到最终 URL（支持多跳）"""
    if not url:
        return url

    # 只处理已知短链域名
    short_domains = ["v.douyin.com", "vm.tiktok.com", "b23.tv", "bili2233.cn"]
    need_resolve = any(d in url for d in short_domains)
    if not need_resolve:
        return url

    try:
        current = url
        for hop in range(3):  # 最多追 3 跳
            req = urllib.request.Request(current, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            })
            resp = urllib.request.urlopen(req, timeout=10, context=_ssl_ctx)
            new_url = resp.url

            if new_url == current:
                break  # 不再跳转

            current = new_url

            # 到达可被 yt-dlp 识别的域名就停
            recognized = ["douyin.com/video/", "bilibili.com/video/", "b23.tv/",
                          "tiktok.com/", "youtube.com/", "youtu.be/"]
            if any(d in current for d in recognized):
                break

        if current != url:
            # 检查是否真的解析到了视频页（而非首页或验证码页）
            video_patterns = ["douyin.com/video/", "bilibili.com/video/", "tiktok.com/", "youtube.com/", "youtu.be/"]
            has_video = any(p in current for p in video_patterns)
            if has_video:
                print(f"[短链解析] {url[:50]} -> {current[:80]}")
                return current
            else:
                print(f"[短链解析] {url[:50]} 重定向到首页/验证码页，无法获取视频ID")
                return None  # 返回 None 表示短链无法自动解析
    except Exception as e:
        print(f"[短链解析失败] {url[:50]}: {e}")

    return url  # 非短链，直接返回

# ─── 路径配置 ────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
BIN_DIR = BASE_DIR / "bin"
SETTINGS_FILE = BASE_DIR / "settings.json"

FFMPEG_PATH = str(BIN_DIR / "ffmpeg.exe")
YTDLP_PATH = str(BIN_DIR / "yt-dlp.exe")

# ─── 设置管理 ─────────────────────────────────────────────────

_DEFAULT_SETTINGS = {
    "download_dir": str(BASE_DIR / "downloads"),
    "filename_template": "{title}",   # 文件名模板（预留）
}

def _load_settings() -> dict:
    """加载设置，合并默认值"""
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return {**_DEFAULT_SETTINGS, **data}
        except Exception:
            pass
    return dict(_DEFAULT_SETTINGS)

def _save_settings(settings: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)

def get_downloads_dir() -> Path:
    """获取当前配置的下载目录，自动创建。如果配置的目录不可写，回退到默认目录"""
    settings = _load_settings()
    configured = settings["download_dir"]
    default = _DEFAULT_SETTINGS["download_dir"]

    if configured != default:
        d = Path(configured)
        try:
            d.mkdir(parents=True, exist_ok=True)
            # 测试写权限（沙箱环境可能不允许写入外部目录）
            test_file = d / ".write_test"
            test_file.write_text("test")
            test_file.unlink()
            return d
        except (PermissionError, OSError) as e:
            print(f"[警告] 无法写入配置目录 {configured}: {e}，已回退到默认目录")

    d = Path(default)
    d.mkdir(parents=True, exist_ok=True)
    return d

# 初始化默认下载目录（兼容旧代码中直接使用 DOWNLOADS_DIR 的地方）
DOWNLOADS_DIR = get_downloads_dir()

# 优先用 bin/ 目录下的 yt-dlp，否则用 pip 安装的
try:
    import yt_dlp
    USE_YTDLP_LIB = True
except ImportError:
    USE_YTDLP_LIB = False

# ─── Flask 应用 ───────────────────────────────────────────────
app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)

# 全局任务字典 {task_id: {...}}
tasks = {}
tasks_lock = threading.Lock()

# ─── 工具函数 ─────────────────────────────────────────────────

def _find_cookie_file(url: str) -> Optional[str]:
    """根据 URL 自动匹配 Cookie 文件"""
    mapping = {
        "douyin.com": "douyin",
        "tiktok.com": "douyin",
        "bilibili.com": "bilibili",
        "youtube.com": "youtube",
        "youtu.be": "youtube",
    }
    for domain, platform in mapping.items():
        if domain in url:
            cfile = BASE_DIR / f"cookies_{platform}.txt"
            if cfile.exists():
                return str(cfile)
    return None


def get_ydl_opts(task_id: str, fmt: str, output_path: str, url: str = ""):
    """根据格式生成 yt-dlp 选项"""

    progress_hooks = [lambda d, tid=task_id: _progress_hook(d, tid)]
    cookie_file = _find_cookie_file(url) if url else None

    is_bilibili = url and ('bilibili.com' in url or 'b23.tv' in url)
    http_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/" if is_bilibili else "https://www.douyin.com/",
    }
    if is_bilibili:
        http_headers["Origin"] = "https://www.bilibili.com"
    common = {
        "outtmpl": output_path,
        "ffmpeg_location": str(BIN_DIR),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": progress_hooks,
        "http_headers": http_headers,
        "extractor_args": {
            "bilibili": {"video_password": []},
        },
    }
    if cookie_file:
        common["cookiefile"] = cookie_file

    if fmt == "mp3":
        return {
            **common,
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        }
    elif fmt == "mp4":
        return {
            **common,
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "postprocessors": [{
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }],
        }
    else:  # original / best
        return {
            **common,
            "format": "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
        }


def _progress_hook(d, task_id):
    """yt-dlp 进度回调"""
    with tasks_lock:
        if task_id not in tasks:
            return
        task = tasks[task_id]

        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed", 0) or 0
            eta = d.get("eta", 0) or 0

            if total > 0:
                pct = round(downloaded / total * 100, 1)
            else:
                pct = task.get("progress", 0)

            task.update({
                "status": "downloading",
                "progress": pct,
                "speed": _fmt_speed(speed),
                "eta": f"{int(eta)}s" if eta else "",
                "size": _fmt_size(total),
            })

        elif d["status"] == "finished":
            task.update({
                "status": "converting",
                "progress": 95,
                "speed": "",
                "eta": "",
            })


def _fmt_speed(bps):
    if not bps:
        return ""
    if bps > 1_000_000:
        return f"{bps/1_000_000:.1f} MB/s"
    elif bps > 1_000:
        return f"{bps/1_000:.0f} KB/s"
    return f"{bps:.0f} B/s"


def _fmt_size(b):
    if not b:
        return ""
    if b > 1_073_741_824:
        return f"{b/1_073_741_824:.1f} GB"
    elif b > 1_048_576:
        return f"{b/1_048_576:.1f} MB"
    elif b > 1_024:
        return f"{b/1_024:.0f} KB"
    return f"{b} B"


def _sanitize_filename(title: str, max_len: int = 120) -> str:
    """将视频标题转换为安全的文件名"""
    if not title or not title.strip():
        return "untitled"
    # 替换 Windows 非法字符
    illegal = r'[\\/:*?"<>|\t\n\r]'
    safe = re.sub(illegal, '_', title)
    # 合并连续下划线和空格
    safe = re.sub(r'[_\s]+', '_', safe)
    # 去掉首尾下划线
    safe = safe.strip('_ ')
    # 限制长度
    if len(safe) > max_len:
        safe = safe[:max_len].rstrip('_')
    return safe or "untitled"


def _run_download(task_id: str, url: str, fmt: str):
    """在后台线程中执行下载"""
    with tasks_lock:
        task = tasks[task_id]

    # 每次下载实时读取设置，支持运行时修改下载目录
    dl_dir = get_downloads_dir()
    ext = "mp3" if fmt == "mp3" else "mp4"

    # 标准化 B站链接：m.bilibili.com -> www.bilibili.com
    url = url.replace('https://m.bilibili.com/', 'https://www.bilibili.com/')
    url = url.replace('http://m.bilibili.com/', 'http://www.bilibili.com/')

    # ── 抖音使用移动端API直接下载 ──
    douyin_match = re.search(r'douyin\.com/video/(\d+)', url)
    if douyin_match and fmt != "mp3":
        try:
            video_id = douyin_match.group(1)
            info_data = _fetch_douyin_mobile(video_id)
            if not info_data:
                raise Exception("获取抖音视频信息失败，请检查Cookie是否有效")
            
            download_url = info_data.get('download_url')
            if not download_url:
                raise Exception("未找到视频下载地址")
            
            # 用视频标题命名文件
            title = info_data.get('title', '') or task_id
            safe_title = _sanitize_filename(title)
            output_path = str(dl_dir / f"{safe_title}.{ext}")

            # 处理重名：加序号后缀
            counter = 1
            base_name = safe_title
            while os.path.exists(output_path):
                safe_title = f"{base_name}_{counter}"
                output_path = str(dl_dir / f"{safe_title}.{ext}")
                counter += 1

            # 下载视频文件（带进度回调）
            import requests as req_lib
            session = _get_douyin_session()
            
            with tasks_lock:
                tasks[task_id].update({
                    "status": "downloading",
                    "progress": 0,
                    "title": info_data.get('title', ''),
                    "speed": "",
                    "eta": "",
                })
            
            # 流式下载
            resp = session.get(download_url, stream=True, timeout=120,
                               headers={'Referer': 'https://www.douyin.com/'})
            resp.raise_for_status()
            total_size = int(resp.headers.get('content-length', 0))
            
            downloaded = 0
            last_update = time.time()
            last_bytes = 0
            
            with open(output_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        # 每秒更新一次进度
                        now = time.time()
                        if now - last_update >= 1.0:
                            speed = (downloaded - last_bytes) / (now - last_update)
                            pct = round(downloaded / total_size * 100, 1) if total_size else 0
                            with tasks_lock:
                                if task_id in tasks:
                                    tasks[task_id].update({
                                        "progress": pct,
                                        "speed": _fmt_speed(speed),
                                        "eta": f"{int((total_size - downloaded) / speed)}s" if speed and total_size else "",
                                        "size": _fmt_size(total_size),
                                    })
                            last_update = now
                            last_bytes = downloaded
            
            # 下载完成
            file_size = os.path.getsize(output_path)
            final_name = f"{safe_title}.{ext}"
            with tasks_lock:
                tasks[task_id].update({
                    "status": "done",
                    "progress": 100,
                    "filename": final_name,
                    "title": info_data.get('title', ''),
                    "file_size": _fmt_size(file_size),
                    "speed": "",
                    "eta": "",
                })
            return
        except Exception as e:
            with tasks_lock:
                tasks[task_id].update({
                    "status": "error",
                    "error": f"抖音下载失败: {str(e)}",
                    "progress": 0,
                })
            return

    # ── 其他平台使用 yt-dlp ──
    # 先获取标题用于文件命名
    try:
        import yt_dlp as ytdlp_lib
        info_opts = {
            "quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "socket_timeout": 20,
        }
        cookie_file = _find_cookie_file(url)
        if cookie_file:
            info_opts["cookiefile"] = cookie_file
        with ytdlp_lib.YoutubeDL(info_opts) as info_ydl:
            pre_info = info_ydl.extract_info(url, download=False)
            raw_title = pre_info.get("title", task_id)
    except Exception:
        raw_title = task_id

    safe_title = _sanitize_filename(raw_title)
    # 重名处理
    counter = 1
    base_name = safe_title
    final_title = safe_title
    while (dl_dir / f"{final_title}.{ext}").exists():
        final_title = f"{base_name}_{counter}"
        counter += 1

    output_tpl = str(dl_dir / f"{final_title}.%(ext)s")
    opts = get_ydl_opts(task_id, fmt, output_tpl, url=url)
    opts["ffmpeg_location"] = str(BIN_DIR)

    try:
        import yt_dlp as ytdlp_lib
        with ytdlp_lib.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", task_id)

        # 找到下载好的文件（用标题名匹配）
        found = None
        expected_name = f"{final_title}.{ext}"
        expected_path = dl_dir / expected_name
        if expected_path.exists():
            found = expected_path
        else:
            # yt-dlp 可能用自己格式化的文件名，搜索匹配
            for f in dl_dir.glob(f"{final_title}*"):
                if f.suffix in (".mp4", ".mp3", ".webm", ".mkv", ".flv"):
                    found = f
                    break

        if found and found.exists():
            # 统一用安全文件名重命名
            final_name = expected_name
            if found.name != final_name:
                try:
                    new_path = dl_dir / final_name
                    os.rename(str(found), str(new_path))
                    found = new_path
                except OSError:
                    final_name = found.name

            with tasks_lock:
                tasks[task_id].update({
                    "status": "done",
                    "progress": 100,
                    "filename": final_name,
                    "title": title,
                    "file_size": _fmt_size(found.stat().st_size),
                    "speed": "",
                    "eta": "",
                })
        else:
            raise FileNotFoundError("下载文件未找到")

    except Exception as e:
        with tasks_lock:
            tasks[task_id].update({
                "status": "error",
                "error": str(e),
                "progress": 0,
            })


# ─── API 路由 ─────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    """获取视频信息（标题、封面、时长）"""
    data = request.get_json()
    url = (data or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "请输入视频链接"}), 400

    # 自动从文本中提取第一个 URL（兼容抖音分享文字场景）
    import re
    url_match = re.search(r'https?://[^\s\u4e00-\u9fa5，。！？、]+', url)
    if url_match:
        url = url_match.group(0).rstrip('）)')

    # 解析短链（v.douyin.com -> douyin.com/video/）
    resolved = resolve_short_link(url)
    if resolved is None:
        return jsonify({"error": "SHORT_LINK_NEED_FULL:抖音短链需要验证码，请粘贴完整的视频链接（如 https://www.douyin.com/video/xxxxx）"}), 400
    url = resolved

    # 标准化 B站链接：m.bilibili.com -> www.bilibili.com（yt-dlp 不认移动端子域名）
    orig = url
    url = url.replace('https://m.bilibili.com/', 'https://www.bilibili.com/')
    url = url.replace('http://m.bilibili.com/', 'http://www.bilibili.com/')
    if url != orig:
        print(f"[B站标准化] {orig[:80]} -> {url[:80]}")

    # ── 抖音使用移动端 API ──
    douyin_match = re.search(r'douyin\.com/video/(\d+)', url)
    if douyin_match:
        video_id = douyin_match.group(1)
        print(f"[Info] 抖音视频 {video_id}, 使用移动端API")
        result = _fetch_douyin_mobile(video_id)
        if result:
            return jsonify(result)
        else:
            return jsonify({"error": "NEED_COOKIE:抖音需要登录 Cookie，请点击「导入 Cookie」按钮"}), 400

    # ── 其他平台使用 yt-dlp ──
    try:
        import yt_dlp as ytdlp_lib
        cookie_file = _find_cookie_file(url)

        # 根据平台设不同的 Referer（B站遇到 douyin Referer 会禁用提取器）
        is_bilibili = 'bilibili.com' in url or 'b23.tv' in url
        http_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/" if is_bilibili else "https://www.douyin.com/",
        }
        if is_bilibili:
            http_headers["Origin"] = "https://www.bilibili.com"
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 20,
            "http_headers": http_headers,
        }
        if cookie_file:
            opts["cookiefile"] = cookie_file
            print(f"[Info] 使用Cookie: {cookie_file}")
        with ytdlp_lib.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        duration = info.get("duration", 0)
        minutes, seconds = divmod(int(duration or 0), 60)

        return jsonify({
            "title": info.get("title", "未知标题"),
            "thumbnail": info.get("thumbnail", ""),
            "uploader": info.get("uploader", info.get("channel", "")),
            "duration": f"{minutes:02d}:{seconds:02d}" if duration else "--",
            "view_count": info.get("view_count", 0),
            "extractor": info.get("extractor_key", ""),
            "url_used": url,
        })
    except Exception as e:
        err_msg = str(e)
        # 友好化常见错误
        if "Unable to download" in err_msg or "urlopen error" in err_msg:
            err_msg = "网络连接失败，请检查网络或稍后重试"
        elif "Unsupported URL" in err_msg:
            err_msg = "不支持此链接格式，请粘贴视频页面的完整网址"
        elif "cookies" in err_msg.lower() or "Fresh cookies" in err_msg:
            err_msg = "NEED_COOKIE:抖音需要登录 Cookie 才能下载，请点击下方「导入 Cookie」按钮"
        elif "Private" in err_msg or "login" in err_msg.lower():
            err_msg = "该视频需要登录才能下载（私密/付费内容）"
        elif "This video is unavailable" in err_msg:
            err_msg = "视频不可用（已删除或地区限制）"
        return jsonify({"error": f"{err_msg}"}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    """发起下载任务"""
    data = request.get_json()
    url = (data or {}).get("url", "").strip()
    fmt = (data or {}).get("format", "mp4").lower()

    if not url:
        return jsonify({"error": "请输入视频链接"}), 400

    # 同样支持从文字中提取 URL
    import re
    url_match = re.search(r'https?://[^\s\u4e00-\u9fa5，。！？、]+', url)
    if url_match:
        url = url_match.group(0).rstrip('）)')

    # 解析短链
    resolved = resolve_short_link(url)
    if resolved is None:
        return jsonify({"error": "SHORT_LINK_NEED_FULL:抖音短链需要验证码，请粘贴完整的视频链接（如 https://www.douyin.com/video/xxxxx）"}), 400
    url = resolved

    # 标准化 B站链接：m.bilibili.com -> www.bilibili.com
    url = url.replace('https://m.bilibili.com/', 'https://www.bilibili.com/')
    url = url.replace('http://m.bilibili.com/', 'http://www.bilibili.com/')

    if fmt not in ("mp4", "mp3", "original"):
        fmt = "mp4"

    task_id = str(uuid.uuid4())[:8]
    with tasks_lock:
        tasks[task_id] = {
            "id": task_id,
            "url": url,
            "format": fmt,
            "status": "pending",
            "progress": 0,
            "speed": "",
            "eta": "",
            "size": "",
            "title": "",
            "filename": "",
            "file_size": "",
            "error": "",
            "created_at": time.time(),
        }

    thread = threading.Thread(target=_run_download, args=(task_id, url, fmt), daemon=True)
    thread.start()

    return jsonify({"task_id": task_id})


@app.route("/api/progress/<task_id>")
def get_progress(task_id):
    """获取任务进度（SSE 流式推送）"""
    def event_stream():
        while True:
            with tasks_lock:
                task = tasks.get(task_id)

            if task is None:
                yield f"data: {json.dumps({'error': '任务不存在'})}\n\n"
                break

            yield f"data: {json.dumps(task)}\n\n"

            if task["status"] in ("done", "error"):
                break

            time.sleep(0.8)

    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/task/<task_id>")
def task_status(task_id):
    """一次性获取任务状态"""
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(task)


@app.route("/api/files")
def list_files():
    """列出所有已下载文件"""
    dl_dir = get_downloads_dir()
    files = []
    for f in sorted(dl_dir.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file():
            with tasks_lock:
                task_id = f.stem
                task = tasks.get(task_id, {})

            files.append({
                "filename": f.name,
                "size": _fmt_size(f.stat().st_size),
                "title": task.get("title", f.stem),
                "format": f.suffix.lstrip(".").upper(),
                "mtime": f.stat().st_mtime,
                "full_path": str(f),
            })
    return jsonify({"files": files, "download_dir": str(dl_dir)})


@app.route("/api/download-file/<filename>")
def download_file(filename):
    """下载已完成的文件"""
    dl_dir = get_downloads_dir()
    file_path = dl_dir / Path(filename).name
    if not file_path.exists():
        return jsonify({"error": "文件不存在"}), 404
    return send_file(str(file_path), as_attachment=True, download_name=filename)


@app.route("/api/delete/<filename>", methods=["DELETE"])
def delete_file(filename):
    """删除文件"""
    dl_dir = get_downloads_dir()
    file_path = dl_dir / Path(filename).name
    if file_path.exists() and file_path.is_file():
        file_path.unlink()
        return jsonify({"ok": True})
    return jsonify({"error": "文件不存在"}), 404


@app.route("/api/settings", methods=["GET"])
def get_settings():
    """获取当前设置，同时返回实际生效的目录"""
    settings = _load_settings()
    actual = get_downloads_dir()
    return jsonify({
        **settings,
        "download_dir_active": str(actual),
        "download_dir_warning": str(actual) != settings["download_dir"],
    })


@app.route("/api/settings", methods=["POST"])
def save_settings():
    """保存设置"""
    data = request.get_json() or {}
    settings = _load_settings()

    # 下载目录
    if "download_dir" in data:
        new_dir = data["download_dir"].strip()
        if not new_dir:
            return jsonify({"error": "目录路径不能为空"}), 400
        p = Path(new_dir)
        # 测试写权限
        try:
            p.mkdir(parents=True, exist_ok=True)
            test_file = p / ".write_test"
            test_file.write_text("test")
            test_file.unlink()
            settings["download_dir"] = str(p.resolve())
        except (PermissionError, OSError) as e:
            return jsonify({"error": f"无法写入此目录（权限被拒绝或沙箱限制）: {e}"}), 400
        except Exception as e:
            return jsonify({"error": f"目录创建失败: {e}"}), 400

    _save_settings(settings)
    actual = get_downloads_dir()
    return jsonify({
        "ok": True,
        "settings": settings,
        "download_dir_active": str(actual),
        "download_dir_warning": str(actual) != settings["download_dir"],
    })


@app.route("/api/cookie-status")
def cookie_status():
    """检查各平台 Cookie 是否存在"""
    result = {}
    for platform in ["douyin", "bilibili"]:
        cfile = BASE_DIR / f"cookies_{platform}.txt"
        result[platform] = cfile.exists()
    return jsonify(result)


@app.route("/api/upload-cookie", methods=["POST"])
def upload_cookie():
    """上传 Cookie 文件（Netscape 格式）"""
    platform = request.form.get("platform", "douyin")
    if platform not in ("douyin", "bilibili", "youtube"):
        return jsonify({"error": "不支持的平台"}), 400

    f = request.files.get("cookie_file")
    if not f:
        return jsonify({"error": "未收到文件"}), 400

    save_path = BASE_DIR / f"cookies_{platform}.txt"
    f.save(str(save_path))
    return jsonify({"ok": True, "path": str(save_path)})


# ─── 启动 ─────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("  B站/抖音 视频下载平台")
    print("  访问: http://localhost:7788")
    print("=" * 50)
    app.run(host="0.0.0.0", port=7788, debug=False, threaded=True)
