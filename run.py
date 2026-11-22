import sys

if sys.argv[1:2] == ["--worker"]:
    from app.worker import main as worker_main
    raise SystemExit(worker_main())

import argparse
import os
from pathlib import Path
from app import updater
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from app import store


def main():
    parser = argparse.ArgumentParser(description="课栈：学习通课程控制台")
    parser.add_argument("--version", action="version", version="课栈 " + updater.build_info()["version"])
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.self_test:
        from app.main import app
        from app import worker, parallel, quiz
        store.initialize()
        assert app and store.unseal(store.seal({"test": True})) == {"test": True}
        return 0
    store.initialize()
    url = f"http://127.0.0.1:{args.port}"
    print(f"\n课栈 · 本地版\n\n打开地址：{url}\n访问口令：{store.token()}\n", flush=True)
    print("请保持此窗口运行。切换账号或关闭网页不会停止任务。\n电脑需保持唤醒；按 Ctrl+C 停止本地应用。账号和记录保存在 .data 目录。\n", flush=True)
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
    from app.main import app

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            if self.started and updater.portable() and store.ROOT.parent.parent == updater.install_root() / ".updates":
                (store.ROOT.parent / "started").touch()

    server = Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False))
    app.state.restart_requested = False
    def restart():
        app.state.restart_requested = True
        server.should_exit = True
    app.state.restart = restart
    server.run()
    return 75 if app.state.restart_requested else (0 if server.started else 1)


if __name__ == "__main__":
    raise SystemExit(main())
