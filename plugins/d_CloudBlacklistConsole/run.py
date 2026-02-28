from app import app
import socket
import sys, os
import time
try:
    PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)
    from Tools.log_helper import project_log, LOG_LEVELS
except Exception:
    def project_log(*args, **kwargs):
        print(*args, **kwargs)

PORT = 2438

def get_local_ips():
    """获取所有本地IPv4地址（兼容多网卡环境）"""
    ips = []
    try:
        # 方法1: 通过UDP连接获取真实IP（兼容WSL）
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))
            ips.append(s.getsockname()[0])
    except:
        pass

    try:
        # 方法2: 遍历所有网络接口
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP):
            ip = info[4][0]
            if ip not in ips and not ip.startswith('127.'):
                ips.append(ip)
    except:
        pass

    return ips or ['127.0.0.1']

def check_port_in_use(port):
    """精准检测本地端口占用"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(('127.0.0.1', port)) == 0

def print_access_urls(port) -> str:
    """生成并打印访问地址"""
    urls = set()
    # 添加本地回环地址
    urls.add(f"http://localhost:{port}")
    # 添加本机IP地址
    for ip in get_local_ips():
        urls.add(f"http://{ip}:{port}")

    lines = []
    lines.append("————————————————————")
    lines.append("🌐 群云黑控制台已在以下地址中打开：")
    lines.append(f"[服务器公网 IP]:{port}")
    
    for url in sorted(urls):
        # 判断内网地址
        is_internal = url.startswith("http://192.168")
        color_code = "" if is_internal else ""
        lines.append(f"{color_code}{url} (通过端口转发)")
    
    lines.append("————————————————————")
    return '\n'.join(lines)

def main() -> str:
    global PORT
    if not check_port_in_use(PORT):
        try:
            app.run(host='0.0.0.0', port=PORT)
        except OSError as e:
            if "Address already in use" in str(e):
                raise OSError(f'''\n💥 群云黑控制台 WebUI 启动失败：端口 {PORT} 被意外占用！
可能原因：1. 本程序已在运行（请检查浏览器或终端）；2. 其他软件占用端口
请执行以下命令释放端口：sudo kill -9 $(lsof -t -i :{PORT})\n''')
    
    return print_access_urls(PORT)

if __name__ == '__main__':
    # 端口占用检测
    if check_port_in_use(PORT):
        project_log(f"⚠️  端口 {PORT} 已被占用！", level=LOG_LEVELS.WARNING)
        project_log("可能原因：")
        project_log("1. 本程序已在运行（请检查浏览器或终端）")
        project_log("2. 其他软件占用端口", level=LOG_LEVELS.WARNING)
        sys.exit(0)
    
    # 打印访问地址
    project_log(print_access_urls(PORT))
    
    # 启动服务
    try:
        app.run(host='0.0.0.0', port=PORT, debug=True)
    except OSError as e:
        if "Address already in use" in str(e):
            project_log("\n💥 启动失败：端口被意外占用！", level=LOG_LEVELS.ERROR)
            project_log(f"请执行以下命令释放端口：sudo kill -9 $(lsof -t -i :{PORT})")
            sys.exit(1)
