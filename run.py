import sys

if sys.argv[1:2] == ["--worker"]:
    from app.worker import main as worker_main
    raise SystemExit(worker_main())

import argparse
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from app import store


def main():
    parser = argparse.ArgumentParser(description="课栈：学习通课程控制台")
    parser.add_argument("--version", action="version", version="课栈 0.6.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    store.initialize()
    url = f"http://127.0.0.1:{args.port}"
    print(f"\n课栈 · macOS 本地版\n\n打开地址：{url}\n访问口令：{store.token()}\n", flush=True)
    print("请保持此窗口运行。切换账号或关闭网页不会停止任务。\nMac 需保持唤醒；按 Ctrl+C 停止本地应用。账号和记录保存在 .data 目录。\n", flush=True)
    if not args.no_browser:
        def open_browser():
            for _ in range(60):
                try:
                    with urllib.request.urlopen(url + "/api/health", timeout=1) as response:
                        if response.status == 200:
                            webbrowser.open(url)
                            return
                except OSError:
                    time.sleep(0.5)
        threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, workers=1,
                proxy_headers=True, forwarded_allow_ips="127.0.0.1,::1", access_log=False)


if __name__ == "__main__":
    main()
