#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
极狐 GitLab 群组批量克隆 - 桌面图形界面版
===========================================
用用户名+密码登录，选择下载根目录，勾选群组后递归克隆所有项目。
本地目录按群组层级自动创建文件夹。仅依赖 Python 自带 Tkinter + 系统 git。

运行：py gitlab_clone_gui.py
"""
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import filedialog, messagebox, simpledialog, ttk

APP_TITLE = "极狐 GitLab 群组批量克隆"
DEFAULT_URL = "http://218.12.70.78:18081"
PER_PAGE = 100
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".gitlab_clone_gui.json")
_LOCAL_PORT = 8841
LOCAL_REDIRECT = f"http://127.0.0.1:{_LOCAL_PORT}/callback"
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

CHECKED, UNCHECKED, HALF = "☑ ", "☐ ", "▣ "
FOLDER, REPO = "📁", "📄"


def sanitize(name: str) -> str:
    return _INVALID.sub("_", name).strip(" .") or "_"


# ---------- 本地 OAuth 回调服务器 ----------
class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """接收 GitLab OAuth 授权后的回调，捕获 ?code=xxx。"""
    server_state = None            # 由 ThreadingHTTPServer 注入

    def log_message(self, *a):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        code = (qs.get("code") or [None])[0]
        error = (qs.get("error") or [None])[0]
        self.server_state["code"] = code
        self.server_state["error"] = error
        self.server_state["done"] = True
        if code:
            body = ("<h2>✅ 已获取授权，可以关闭本页并返回程序</h2>"
                    "<p>正在程序内登录…</p>").encode()
            status = 200
        else:
            body = ("<h2>❌ 授权失败</h2>"
                    f"<p>错误：{error} 请关闭本页返回程序查看原因。</p>").encode()
            status = 400
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()


def start_callback_server(port: int):
    """在后台线程启动回调 HTTP 服务器，返回 (状态字典, 停止函数)。"""
    state = {"code": None, "error": None, "done": False}
    handler = type("BoundHandler", (_CallbackHandler,), {"server_state": state})

    class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    srv = _Server(("127.0.0.1", port), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return state, srv.shutdown


# ---------- 下载实时监视（内置到主程序） ----------
def count_cloned(out_root: str) -> int:
    """统计下载根目录下已完成的克隆数（含 .git 的目录）。"""
    if not out_root or not os.path.isdir(out_root):
        return 0
    n = 0
    for _root, dirs, _files in os.walk(out_root):
        if ".git" in dirs:
            n += 1
    return n


def disk_usage_gb(out_root: str) -> float:
    """下载根目录磁盘占用（GB），失败返回 0。"""
    if not out_root or not os.path.isdir(out_root):
        return 0.0
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
             f"(Get-ChildItem -Recurse -Force '{out_root}' | "
             f"Measure-Object -Property Length -Sum).Sum / 1GB"],
            capture_output=True, text=True, timeout=90, encoding="utf-8",
            errors="replace")
        return float((r.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def transferring_repos() -> list:
    """从 git 进程命令行提取当前正在传输的仓库路径。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
             "Get-CimInstance Win32_Process -Filter \"name='git.exe'\" | "
             "Select-Object -ExpandProperty CommandLine"],
            capture_output=True, text=True, timeout=30, encoding="utf-8",
            errors="replace")
        repos = []
        for line in (r.stdout or "").splitlines():
            if "remote-http" in line and ".git" in line:
                url = line.strip().rsplit(" ", 1)[-1]
                repos.append(url.split("@", 1)[-1])
        return sorted(set(repos))
    except Exception:
        return []


def save_config(data: dict):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


TREE_CACHE_PATH = os.path.join(os.path.expanduser("~"), ".gitlab_tree_cache.json")


def save_tree_cache(cache: dict):
    """把整棵群组树缓存到本地磁盘（结构数据，不含 token）。"""
    try:
        with open(TREE_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass


def load_tree_cache() -> dict:
    try:
        with open(TREE_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


class GitLabClient:
    """极简 GitLab API 客户端（浏览器授权登录 + 分页处理）。"""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.api = self.base + "/api/v4"
        self.token = ""

    def set_token(self, token: str):
        self.token = (token or "").strip()

    def verify_token(self) -> bool:
        """校验当前 token 是否有效。有效返回 True，否则抛异常或返回 False。"""
        try:
            json.loads(self._get("/user").decode())
            return True
        except urllib.error.HTTPError as e:
            return e.code != 401
        except Exception:
            return False

    def login(self, username: str, password: str) -> str:
        """通过 OAuth2 密码模式换取访问令牌（仅适用于未开 2FA 的账号）。"""
        data = urllib.parse.urlencode({
            "grant_type": "password",
            "username": username,
            "password": password,
        }).encode()
        req = urllib.request.Request(self.base + "/oauth/token", data=data,
                                     method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
        self.token = body.get("access_token", "")
        if not self.token:
            raise RuntimeError("登录成功但未返回 token")
        return self.token

    def exchange_code(self, code: str, client_id: str, client_secret: str = "") -> str:
        """用授权码换访问令牌（浏览器授权码模式，支持 2FA）。
        机密型应用需提供 client_secret。"""
        payload = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": LOCAL_REDIRECT,
        }
        if client_secret:
            payload["client_secret"] = client_secret
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(self.base + "/oauth/token", data=data,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"换取令牌失败（HTTP {e.code}）")
        self.token = body.get("access_token", "")
        if not self.token:
            raise RuntimeError("换取令牌成功但未返回 access_token")
        return self.token

    def _headers(self):
        return {"PRIVATE-TOKEN": self.token, "User-Agent": "gitlab-clone-gui"}

    def _get(self, path: str, params: dict = None, with_total: bool = False):
        qs = urllib.parse.urlencode(params or {})
        full = f"{self.api}{path}" + (f"?{qs}" if qs else "")
        req = urllib.request.Request(full, headers=self._headers())
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            if with_total:
                total = resp.headers.get("X-Total")
                return body, (int(total) if total else None)
            return body

    def _paginate(self, path: str, params: dict = None) -> list:
        """分页拉取。第 1 页拿到总数后，剩余页并发拉取，减少串行等待。"""
        params = dict(params or {})
        params["per_page"] = PER_PAGE
        params["page"] = 1
        body, total = self._get(path, params, with_total=True)
        result = json.loads(body.decode())
        if total is None:
            # 服务器没给总数，退回逐页串行
            while len(result) >= PER_PAGE:
                params["page"] += 1
                nxt = json.loads(self._get(path, params).decode())
                if not nxt:
                    return result
                result.extend(nxt)
            return result
        pages = -(-total // PER_PAGE)   # ceil
        if pages <= 1:
            return result

        from concurrent.futures import ThreadPoolExecutor
        rest_params = dict(params)
        with ThreadPoolExecutor(max_workers=min(pages - 1, 8)) as pool:
            futs = [pool.submit(lambda pg: json.loads(self._get(
                path, {**rest_params, "page": pg}).decode()), pg)
                for pg in range(2, pages + 1)]
            for f in futs:
                result.extend(f.result())
        return result

    def top_groups(self) -> list:
        """返回所有顶级群组。

        注意：/api/v4/groups 默认返回全部群组（含所有嵌套子群组），
        且部分服务器会忽略 top_level 参数，因此这里用 parent_id 为空来过滤，
        只保留真正的最外层群组。
        """
        return [g for g in self._paginate("/groups")
                if g.get("parent_id") is None]

    def group_children(self, group_id) -> tuple:
        """返回 (子群组列表, 直属项目列表)。两个请求并发发出。"""
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_subs = pool.submit(self._paginate, f"/groups/{group_id}/subgroups")
            f_proj = pool.submit(
                self._paginate, f"/groups/{group_id}/projects",
                {"simple": "true"})
            subs = f_subs.result()
            projects = f_proj.result()
        return subs, projects

    def build_clone_url(self, project: dict) -> str:
        """构造可访问的克隆 URL。

        服务器返回的 http_url_to_repo 常指向内网地址（如 192.168.x.x），本机连不通；
        因此取其路径（group/project.git），拼上本程序实际访问的 self.base，
        userinfo 用 oauth2:token 放在 host 之前，实现免交互克隆。
        """
        repo = project["http_url_to_repo"]
        path = repo.split("://", 1)[1].split("/", 1)[1]   # group/proj.git
        scheme, hostport = self.base.split("://", 1)
        quoted = urllib.parse.quote(self.token, safe="")
        return f"{scheme}://oauth2:{quoted}@{hostport}/{path}"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.client = None
        self.nodes = {}      # tree iid -> {kind:'group'|'project', id, path, proj?}
        self.checked = set() # 已勾选的 tree iid
        self._prefetch = {}  # group_id -> (subs, projects) 预取缓存
        self._full_tree = False   # True=整棵树已建好(全树缓存模式)
        self.log_queue = []
        self._callback_srv = None
        self._callback_state = None
        root.title(APP_TITLE)
        root.geometry("900x640")
        self._build_ui()
        self._load_saved()

    # ---------- 界面构建 ----------
    def _build_ui(self):
        cfg = load_config()
        pad = {"padx": 6, "pady": 4}

        top = ttk.LabelFrame(self.root, text="服务器与登录")
        top.pack(fill="x", padx=8, pady=6)

        ttk.Label(top, text="服务器地址").grid(row=0, column=0, sticky="e", **pad)
        self.var_url = tk.StringVar(value=cfg.get("url", DEFAULT_URL))
        ttk.Entry(top, textvariable=self.var_url, width=50).grid(row=0, column=1, sticky="we", **pad)

        ttk.Label(top, text="用户名").grid(row=1, column=0, sticky="e", **pad)
        self.var_user = tk.StringVar(value=cfg.get("user", ""))
        ttk.Entry(top, textvariable=self.var_user, width=50).grid(row=1, column=1, sticky="we", **pad)

        ttk.Label(top, text="密码").grid(row=2, column=0, sticky="e", **pad)
        self.var_pass = tk.StringVar()
        self.pass_frame = ttk.Frame(top)
        self.pass_frame.grid(row=2, column=1, sticky="we", **pad)
        self.ent_pass = ttk.Entry(self.pass_frame, textvariable=self.var_pass, show="*")
        self.ent_pass.pack(side="left", fill="x", expand=True)
        self.btn_show_pass = ttk.Button(self.pass_frame, text="👁", width=4,
                                        command=self.toggle_show_pass)
        self.btn_show_pass.pack(side="left", padx=(4, 0))
        self._show_pass = False

        ttk.Label(top, text="下载目录").grid(row=3, column=0, sticky="e", **pad)
        self.var_out = tk.StringVar(value=cfg.get("out", ""))
        ttk.Entry(top, textvariable=self.var_out, width=50).grid(row=3, column=1, sticky="we", **pad)
        ttk.Button(top, text="浏览…", command=self._choose_dir).grid(row=3, column=2, **pad)
        top.columnconfigure(1, weight=1)

        btns = ttk.Frame(self.root)
        btns.pack(fill="x", padx=8, pady=4)
        self.btn_login = ttk.Button(btns, text="用户名密码登录", command=self.on_login)
        self.btn_login.pack(side="left", padx=4)
        self.btn_browser = ttk.Button(btns, text="浏览器授权登录",
                                      command=self.on_browser_login)
        self.btn_browser.pack(side="left", padx=4)
        self.btn_paste = ttk.Button(btns, text="粘贴Token", command=self.on_paste_token)
        self.btn_paste.pack(side="left", padx=4)
        self.btn_check = ttk.Button(btns, text="全选 / 全不选", command=self.toggle_all, state="disabled")
        self.btn_check.pack(side="left", padx=8)
        ttk.Label(btns, text="（☑全选 ▣部分 ☐未选，勾父=全选子级）").pack(side="left", padx=2)
        self.btn_refresh = ttk.Button(btns, text="🔄 刷新群组树", command=self.on_refresh_tree, state="disabled")
        self.btn_refresh.pack(side="left", padx=4)
        self.btn_dl = ttk.Button(btns, text="开始下载勾选项", command=self.on_download, state="disabled")
        self.btn_dl.pack(side="left", padx=4)
        self.btn_open = ttk.Button(btns, text="打开下载目录", command=self._open_outdir, state="disabled")
        self.btn_open.pack(side="left", padx=4)
        self.lbl_stat = ttk.Label(btns, text="未登录")
        self.lbl_stat.pack(side="right", padx=4)

        tree_frame = ttk.Frame(self.root)
        tree_frame.pack(fill="both", expand=True, padx=8)
        self.tree = ttk.Treeview(tree_frame, columns=("name",), show="tree", height=12)
        self.tree.heading("#0", text="勾选 / 群组 / 项目")
        self.tree.column("#0", width=700)
        ys = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_open)

        bottom = ttk.Notebook(self.root)
        bottom.pack(fill="both", expand=True, padx=8, pady=6)

        # 页签1: 下载进度（实时监视）
        prog_frame = ttk.Frame(bottom, padding=6)
        bottom.add(prog_frame, text=" 下载进度 ")
        self.progress = ttk.Progressbar(prog_frame, maximum=100)
        self.progress.pack(fill="x")
        self.lbl_prog_done = ttk.Label(prog_frame, text="已完成: -", font=("Microsoft YaHei UI", 11, "bold"))
        self.lbl_prog_done.pack(anchor="w", pady=(4, 0))
        self.lbl_prog_size = ttk.Label(prog_frame, text="磁盘占用: -", font=("Microsoft YaHei UI", 10))
        self.lbl_prog_size.pack(anchor="w")
        self.lbl_prog_run = ttk.Label(prog_frame, text="正在克隆: -", font=("Microsoft YaHei UI", 10))
        self.lbl_prog_run.pack(anchor="w")
        ttk.Label(prog_frame, text="当前传输中的仓库：",
                  font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(6, 0))
        self.txt_transferring = tk.Text(prog_frame, height=6, state="disabled",
                                        wrap="none", font=("Consolas", 9))
        self.txt_transferring.pack(fill="both", expand=True)

        # 页签2: 日志
        log_frame = ttk.Frame(bottom, padding=6)
        bottom.add(log_frame, text=" 日志 ")
        self.log_text = tk.Text(log_frame, height=10, state="disabled", wrap="word",
                                font=("Microsoft YaHei UI", 9))
        self.log_text.pack(fill="both", expand=True)
        self.notebook = bottom
        self._monitor_on = False
        self._monitor_prev = {"done": 0, "size": 0.0}

    def _load_saved(self):
        cfg = load_config()
        # 若有已保存的 token，启动时自动登录并加载群组
        token = cfg.get("token", "")
        if token and cfg.get("url"):
            self.var_url.set(cfg["url"])
            c = GitLabClient(cfg["url"])
            c.set_token(token)
            self.client = c
            self.set_status("自动登录中…")
            self.log("检测到已保存的登录凭证，正在自动登录…")
            threading.Thread(target=self._load_groups_after_login, daemon=True).start()

    def toggle_show_pass(self):
        self._show_pass = not self._show_pass
        self.ent_pass.config(show="" if self._show_pass else "*")

    def _choose_dir(self):
        d = filedialog.askdirectory(title="选择下载根目录")
        if d:
            self.var_out.set(os.path.normpath(d))

    def _open_outdir(self):
        d = self.var_out.get().strip()
        if d and os.path.isdir(d):
            os.startfile(d)  # noqa  # Windows
        else:
            messagebox.showinfo("提示", "请先选择有效的下载目录")

    # ---------- 日志 ----------
    def log(self, msg: str):
        self.log_queue.append(msg + "\n")
        self.root.after(0, self._flush_log)

    def _flush_log(self):
        if not self.log_queue:
            return
        self.log_text.configure(state="normal")
        for m in self.log_queue:
            self.log_text.insert("end", m)
            self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.log_queue.clear()

    def set_status(self, text: str):
        self.lbl_stat.config(text=text)

    # ---------- 登录与加载 ----------
    def on_login(self):
        url = self.var_url.get().strip().rstrip("/")
        user = self.var_user.get().strip()
        pwd = self.var_pass.get()
        if not url or not user or not pwd:
            messagebox.showwarning("提示", "请填写服务器地址、用户名和密码")
            return
        out = self.var_out.get().strip()
        if out and not os.path.isdir(out):
            messagebox.showwarning("提示", "下载目录不存在，请用「浏览」选择一个有效文件夹")
            return

        self.btn_login.config(state="disabled")
        self.set_status("登录中…")
        threading.Thread(target=self._do_login, args=(url, user, pwd), daemon=True).start()

    def _do_login(self, url, user, pwd):
        try:
            client = GitLabClient(url)
            client.login(user, pwd)
            self.client = client
            self.log("✅ 登录成功")
            self.log("正在加载群组列表…")
            groups = client.top_groups()
            self.log(f"找到 {len(groups)} 个顶级群组")
            self.root.after(0, lambda: self._populate(groups))
            save_config({"url": url, "user": user, "out": self.var_out.get().strip(),
                         "checked": []})
        except urllib.error.HTTPError as e:
            if e.code == 401:
                msg = ("用户名/密码错误，或该账号开启了双重验证(2FA)。\n"
                       "如果开启了2FA，请改用「浏览器授权登录」或「粘贴Token」按钮。")
            elif e.code == 403:
                msg = "登录被拒绝（403），请检查权限。"
            else:
                msg = f"HTTP {e.code}"
            self.root.after(0, lambda: messagebox.showerror("登录失败", msg))
            self.set_status("登录失败")
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("登录失败", str(e)))
            self.set_status("登录失败")
        finally:
            self.root.after(0, lambda: self.btn_login.config(state="normal"))

    def on_browser_login(self):
        """浏览器授权码流程：打开极狐授权页，用户登录授权后本地回调捕获 code。"""
        url = self.var_url.get().strip().rstrip("/")
        if not url:
            messagebox.showwarning("提示", "请先填写服务器地址")
            return

        show_step = (
            "浏览器授权登录需要先配置一个极狐 OAuth 应用（只需一次）。\n\n"
            "请按以下步骤操作：\n"
            "1. 用浏览器登录极狐 GitLab\n"
            "2. 打开【右上角头像 → Preferences → Applications】\n"
            "3. 点击「New application」，填写：\n"
            "   · 名称：任意，如 clone-tool\n"
            "   · Redirect URI：http://127.0.0.1:8841/callback\n"
            "   · 勾选 scope：api 和 read_user\n"
            "   · 其他默认，点 Save application\n"
            "4. 保存后会显示 Application ID 和 Secret 两串字符\n"
            "   请把这两个都粘贴到程序（Secret 只显示一次，注意复制保存）\n\n"
            "如果不想用这种方式，可改用「粘贴Token」按钮。"
        )
        got_id = simpledialog.askstring(
            "浏览器授权登录 - 第1步", show_step + "\n\n请输入 Application ID：",
            initialvalue=load_config().get("client_id", ""))
        if not got_id or not got_id.strip():
            return
        client_id = got_id.strip()
        got_secret = simpledialog.askstring(
            "浏览器授权登录 - 第2步",
            "请输入 Application 的 Secret（机密型应用必填；非机密可为空）：\n"
            "（创建应用时页面显示的那串 Secret，只显示一次）",
            initialvalue=load_config().get("client_secret", ""), show="*")
        client_secret = (got_secret or "").strip()

        # 检查端口占用
        if self._callback_srv is not None:
            try:
                self._callback_srv.shutdown()
            except Exception:
                pass
            self._callback_srv = None
        try:
            state, shutdown = start_callback_server(_LOCAL_PORT)
            self._callback_srv = shutdown
            self._callback_state = state
        except OSError as e:
            messagebox.showerror("错误", f"无法启动本地回调服务（端口 {_LOCAL_PORT} 被占用？）：\n{e}")
            return

        params = urllib.parse.urlencode({
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": LOCAL_REDIRECT,
            "scope": "api read_user",
        })
        auth_url = f"{url}/oauth/authorize?{params}"
        self.btn_browser.config(state="disabled")
        self.set_status("请在浏览器中登录并授权…")
        self.log("已打开浏览器授权页，请在浏览器中登录并点击「授权」。")
        webbrowser.open(auth_url)

        save_config({**load_config(), "client_id": client_id,
                     "client_secret": client_secret})

        threading.Thread(target=self._wait_callback,
                         args=(client_id, client_secret), daemon=True).start()

    def _wait_callback(self, client_id, client_secret):
        # 轮询等待用户在浏览器完成授权回调（最多 3 分钟）
        waited = 0
        state = self._callback_state
        while waited < 180 and not (state and state["done"]):
            threading.Event().wait(0.5)
            waited += 0.5
        try:
            if self._callback_srv is not None:
                self._callback_srv()
                self._callback_srv = None
        except Exception:
            pass

        if not state or not state["done"]:
            self.root.after(0, lambda: [messagebox.showwarning("超时", "等待授权超时，请重试。"),
                                        self.set_status("授权超时")])
            self.root.after(0, lambda: self.btn_browser.config(state="normal"))
            return
        if not state["code"]:
            self.root.after(0, lambda: [messagebox.showerror("授权失败", f"授权被拒绝：{state.get('error')}"),
                                        self.set_status("授权失败")])
            self.root.after(0, lambda: self.btn_browser.config(state="normal"))
            return

        try:
            c = GitLabClient(self.var_url.get().strip().rstrip("/"))
            c.exchange_code(state["code"], client_id, client_secret)
            self.client = c
            self.log("✅ 浏览器授权登录成功")
            self.root.after(0, lambda: self._load_groups_after_login())
        except Exception as e:
            self.root.after(0, lambda: [messagebox.showerror("登录失败", f"换取令牌失败：\n{e}"),
                                        self.set_status("登录失败")])
            self.root.after(0, lambda: self.btn_browser.config(state="normal"))

    def on_paste_token(self):
        """手动粘贴 Personal Access Token 登录。"""
        token = simpledialog.askstring("粘贴Token",
                                       "把极狐的 Personal Access Token 粘贴到这里：\n"
                                       "（获取：右上角头像 → Preferences → Access Tokens 创建，勾选 api/read_api/read_repository）",
                                       show="*")
        if not token or not token.strip():
            return
        c = GitLabClient(self.var_url.get().strip().rstrip("/"))
        c.set_token(token.strip())
        self.client = c
        self.btn_paste.config(state="disabled")
        self.set_status("校验 token…")
        self.log("正在校验 token…")
        threading.Thread(target=self._load_groups_after_login, args=(), daemon=True).start()

    def _load_groups_after_login(self):
        try:
            # 1) 先用磁盘缓存瞬间建出整棵树（如有）
            cache = load_tree_cache()
            url = self.var_url.get().strip().rstrip("/")
            tree = cache.get(url)
            if tree and tree.get("groups"):
                self.log("📦 从本地缓存加载群组树（秒开）…")
                groups = tree["groups"]
                children = {int(k): v for k, v in tree.get("children", {}).items()}
                self._populate(groups, children)
                self.set_status(f"已从缓存加载 {len(groups)} 个顶级群组，后台正在同步最新…")
                # 后台刷新全树
                threading.Thread(target=self._refresh_full_tree,
                                 args=(url, False), daemon=True).start()
                save_config({**load_config(), "url": url,
                             "out": self.var_out.get().strip(),
                             "token": self.client.token})
                return

            # 2) 无缓存：在线加载（首次）
            self.log("首次加载：正在拉取全部群组结构（约30秒，之后秒开）…")
            self._refresh_full_tree(url, first=True)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self.root.after(0, lambda: messagebox.showerror("失败", "token 无效或无权限（401）。请重新获取 token。"))
                self.set_status("token 无效")
            else:
                self.root.after(0, lambda: messagebox.showerror("失败", f"HTTP {e.code}"))
                self.set_status("失败")
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("失败", str(e)))
            self.set_status("失败")
        finally:
            self.root.after(0, lambda: [self.btn_browser.config(state="normal"),
                                        self.btn_paste.config(state="normal")])

    def on_refresh_tree(self):
        """手动刷新：重新拉取最新群组树（新项目/新群组会进来）。

        刷新采用增量合并：不重建树，勾选和展开状态原样保留。
        """
        if not self.client:
            messagebox.showinfo("提示", "请先登录")
            return
        self.btn_refresh.config(state="disabled")
        self.set_status("刷新群组树中…（后台并发拉取，约30-60秒）")
        self.log("🔄 手动刷新：正在拉取最新群组树…（勾选和展开状态保持不变）")

        url = self.var_url.get().strip().rstrip("/")
        def _job():
            self._refresh_full_tree(url, first=False)
            self.root.after(0, lambda: self.btn_refresh.config(state="normal"))
        threading.Thread(target=_job, daemon=True).start()

    def _refresh_full_tree(self, url, first=False):
        """并发抓取全部群组树结构，写入磁盘缓存并重建树。"""
        # 防并发：已在刷新中则跳过本次
        if getattr(self, "_refreshing", False):
            self.log("刷新已在进行中，跳过重复触发")
            return
        self._refreshing = True
        try:
            self._refresh_full_tree_inner(url, first)
        finally:
            self._refreshing = False

    def _refresh_full_tree_inner(self, url, first=False):
        """并发抓取全部群组树结构，写入磁盘缓存并重建树。"""
        try:
            all_groups = self.client._paginate("/groups")
            tops = [g for g in all_groups if g.get("parent_id") is None]
            # 需要查子级的群组: 全部（失败自动重试2轮，防止网络抖动漏群组）
            ids = [g["id"] for g in all_groups]
            children = {}
            from concurrent.futures import ThreadPoolExecutor
            remaining = list(ids)
            for _round in range(3):
                if not remaining:
                    break
                failed = []
                with ThreadPoolExecutor(max_workers=12) as pool:
                    futs = {pool.submit(self.client.group_children, gid): gid
                            for gid in remaining}
                    for fut in as_completed(futs):
                        gid = futs[fut]
                        try:
                            children[gid] = fut.result()
                        except Exception:
                            failed.append(gid)
                remaining = failed
                if remaining:
                    self.log(f"⚠️ {len(remaining)} 个群组拉取失败，重试中…")
            # 只保留树需要的精简字段，缓存体积小
            slim_tops = [{"id": g["id"], "name": g["name"],
                          "full_path": g["full_path"]} for g in tops]
            slim_children = {}
            for gid, (subs, projs) in children.items():
                slim_children[str(gid)] = (
                    [{"id": s["id"], "name": s["name"], "full_path": s["full_path"]}
                     for s in subs],
                    [{"id": p["id"], "name": p["name"],
                      "path_with_namespace": p["path_with_namespace"],
                      "http_url_to_repo": p["http_url_to_repo"]}
                     for p in projs],
                )
            save_tree_cache({url: {"groups": slim_tops, "children": slim_children}})
            self.log(f"✅ 全部 {len(ids)} 个群组结构已缓存到本地，下次启动秒开")
            # 增量合并进现有树：不重建、保持展开状态和勾选不动
            self.root.after(0, lambda: self._merge_tree(slim_tops, slim_children))
            if not first:
                self.set_status(f"已同步最新群组树（{len(slim_tops)} 个顶级群组）")
            else:
                save_config({**load_config(), "url": url,
                             "out": self.var_out.get().strip(),
                             "token": self.client.token})
        except Exception as e:
            self.log(f"⚠️ 全树刷新失败：{e}")
            if first:
                # 首次失败：退回只加载顶级群组
                try:
                    tops = self.client.top_groups()
                    slim = [{"id": g["id"], "name": g["name"],
                             "full_path": g["full_path"]} for g in tops]
                    self.root.after(0, lambda: self._populate(slim, {}))
                except Exception:
                    pass

    def _merge_tree(self, tops, children):
        """把刷新后的数据增量合并进现有树。

        与 _populate(重建)不同：不动已存在的节点（保持展开状态、勾选、位置），
        只做三件事：新增服务器上新出现的节点、更新群组名、缓存里缺失的补占位。
        """
        def merge_under(parent_iid, glist):
            existing = {self.nodes[c]["id"]: c
                        for c in self.tree.get_children(parent_iid)
                        if c in self.nodes and self.nodes[c]["kind"] == "group"}
            for g in glist:
                gid = g["id"]
                if gid in existing:
                    giid = existing[gid]
                    # 更新群组名（若改名）
                    if self.tree.item(giid, "text").find(g["name"]) < 0:
                        mark = CHECKED if giid in self.checked else \
                            (HALF if HALF in self.tree.item(giid, "text") else UNCHECKED)
                        self.tree.item(giid, text=f"{mark}{FOLDER} {g['name']}")
                else:
                    # 新群组：插入（不勾选，保守处理）
                    giid = self._insert_group(parent_iid, g, gid, g["full_path"],
                                              lazy=False)
                entry = children.get(str(gid)) or children.get(gid)
                if entry:
                    subs, projs = entry
                    # 项目：新增缺失的
                    cur_p = {self.nodes[c]["id"] for c in self.tree.get_children(giid)
                             if c in self.nodes and self.nodes[c]["kind"] == "project"}
                    for p in projs:
                        if p["id"] not in cur_p:
                            p_iid = f"p{p['id']}"
                            if self.tree.exists(p_iid):
                                continue
                            parent_checked = giid in self.checked
                            mark = CHECKED if parent_checked else UNCHECKED
                            self.tree.insert(giid, "end", iid=p_iid,
                                             text=f"   {mark}{REPO} {p['name']}",
                                             values=(p["http_url_to_repo"], ""))
                            self.nodes[p_iid] = {"kind": "project", "id": p["id"],
                                                 "path": p["path_with_namespace"],
                                                 "proj": p, "parent": giid}
                            if parent_checked:
                                self.checked.add(p_iid)
                    # 递归子群组
                    merge_under(giid, subs)
        merge_under("", tops)
        n_new = len(self.nodes)
        self.set_status(f"已同步最新群组树（共 {n_new} 个节点），勾选和展开状态保持不变")

    def _populate(self, groups, children=None):
        """建树。给了 children（缓存/全树数据）就直接建整棵树，全部秒开。"""
        # 彻底清空旧树（防止后台刷新重建时 iid 残留冲突）
        for iid in list(self.tree.get_children("")):
            self.tree.delete(iid)
        self.nodes.clear()
        self.checked.clear()
        children = children or {}
        self._full_tree = bool(children)   # 有子级数据=全树模式

        def build(parent_iid, glist):
            for g in glist:
                # 全树构建：子级马上由下方代码插入，不需要占位（lazy=False）
                giid = self._insert_group(parent_iid, g, g["id"], g["full_path"],
                                          lazy=False)
                entry = children.get(int(g["id"]))
                if entry:
                    subs, projs = entry
                    for p in projs:
                        p_iid = f"p{p['id']}"
                        if self.tree.exists(p_iid):
                            self.tree.delete(p_iid)
                        mark = CHECKED if p_iid in self.checked else UNCHECKED
                        self.tree.insert(giid, "end", iid=p_iid,
                                         text=f"   {mark}{REPO} {p['name']}",
                                         values=(p["http_url_to_repo"], ""))
                        self.nodes[p_iid] = {"kind": "project", "id": p["id"],
                                             "path": p["path_with_namespace"],
                                             "proj": p, "parent": giid}
                    build(giid, subs)
                else:
                    # 该群组子级数据缺失（拉取时失败）：插占位，展开时懒加载兜底
                    ph = f"ph-{g['id']}"
                    if self.tree.exists(ph):
                        self.tree.delete(ph)
                    self.tree.insert(giid, "end", iid=ph,
                                     text="", values=("", ""))
                    self._full_tree = False  # 退回混合模式：允许懒加载补缺
        build("", groups)
        self.btn_check.config(state="normal")
        self.btn_dl.config(state="normal")
        self.btn_refresh.config(state="normal")
        n_all = len(self.nodes)
        self.set_status(f"已加载树：共 {n_all} 个节点（群组+项目），点行勾选，开始下载")

    def _insert_group(self, parent_iid, g, group_id, path, lazy=True) -> str:
        """插入群组节点。lazy=True 表示该群组子级数据未知（懒加载模式），
        插入空白占位让箭头出现；lazy=False 表示子级马上会由调用方插入（全树构建）。"""
        iid = f"g{group_id}"
        # 防御：重建树时同名 iid 可能残留，先删再插
        if self.tree.exists(iid):
            self.tree.delete(iid)
        # 新插入的节点继承父级的勾选状态（父级勾选=其下全选）
        parent_checked = bool(parent_iid) and parent_iid in self.checked
        mark = CHECKED if (parent_checked or iid in self.checked) else UNCHECKED
        self.tree.insert(parent_iid, "end", iid=iid,
                         text=f"{mark}{FOLDER} {g['name']}")
        self.nodes[iid] = {"kind": "group", "id": group_id, "path": path,
                           "parent": parent_iid or None}
        if parent_checked:
            self.checked.add(iid)
        if lazy:
            ph = f"ph-{group_id}"
            if self.tree.exists(ph):
                self.tree.delete(ph)
            self.tree.insert(iid, "end", iid=ph,
                             text="", values=("", ""))
        return iid

    def _on_tree_open(self, _event):
        # 全树模式：所有内容已建好，展开无需加载
        if self._full_tree:
            return
        # 懒加载模式：遍历所有群组节点，找出「已展开但仍只有占位子节点」的去真正加载。
        for iid, node in list(self.nodes.items()):
            if node["kind"] != "group" or not self.tree.exists(iid) \
                    or not self.tree.item(iid, "open"):
                continue
            children = self.tree.get_children(iid)
            if not children or all(c.startswith("ph-") for c in children):
                gid = node["id"]
                cached = self._prefetch.get(gid)
                if cached is not None:
                    # 预取缓存命中：立即填充，无需等待
                    subs, projects = cached
                    self.tree.delete(*children)
                    for p in projects:
                        p_iid = f"p{p['id']}"
                        mark = CHECKED if (p_iid in self.checked or iid in self.checked) else UNCHECKED
                        self.tree.insert(iid, "end", iid=p_iid,
                                         text=f"   {mark}{REPO} {p['name']}",
                                         values=(p["http_url_to_repo"], ""))
                        self.nodes[p_iid] = {"kind": "project", "id": p["id"],
                                             "path": p["path_with_namespace"],
                                             "proj": p, "parent": iid}
                        if mark == CHECKED:
                            self.checked.add(p_iid)
                    for s in subs:
                        self._insert_group(iid, s, s["id"], s["full_path"])
                else:
                    self.tree.delete(*children)
                    self._start_loading_anim(iid, gid)
                    threading.Thread(target=self._load_group_children,
                                     args=(iid, gid), daemon=True).start()

    def _start_loading_anim(self, iid, gid):
        """展开后的加载动画：⟳ 每150ms 转一格，数据到达自动停止。"""
        anim_iid = f"anim-{gid}"
        if self.tree.exists(anim_iid):
            self.tree.delete(anim_iid)
        self.tree.insert(iid, "end", iid=anim_iid,
                         text=f"   {REPO} ⟳ 正在加载群组内容…", values=("", ""))

        def spin(frame=[0]):
            if not self.tree.exists(anim_iid):
                return  # 数据已到达并被清掉，动画自然停止
            frame[0] = (frame[0] + 1) % 4
            spins = "⟳⟲⟳⟲"
            self.tree.item(anim_iid, text=f"   {REPO} {spins[frame[0]]} 正在加载群组内容…")
            self.root.after(150, spin)
        spin()

    def _fill_children_anim(self, iid, subs, projects):
        """数据到达后停止动画并级联填充子级（有淡入感，不生硬）。"""
        parent_checked = iid in self.checked
        # 清掉加载动画行和残留占位
        for c in self.tree.get_children(iid):
            if c.startswith("anim-") or c.startswith("ph-") \
                    or "加载中" in self.tree.item(c, "text"):
                self.tree.delete(c)
        items = []
        for p in projects:
            p_iid = f"p{p['id']}"
            mark = CHECKED if (parent_checked or p_iid in self.checked) else UNCHECKED
            items.append((p_iid, f"   {mark}{REPO} {p['name']}",
                          p["http_url_to_repo"], "project", p))
        for s in subs:
            items.append((f"g{s['id']}", None, None, "group", s))

        state = {"i": 0}
        def step():
            # 每帧插2个节点，20ms 一帧 = 100节点约1秒的瀑布感
            for _ in range(2):
                if state["i"] >= len(items):
                    return
                node_iid, text, url, kind, obj = items[state["i"]]
                state["i"] += 1
                if kind == "project":
                    if self.tree.exists(node_iid):
                        continue
                    self.tree.insert(iid, "end", iid=node_iid, text=text,
                                     values=(url, ""))
                    mark = CHECKED if node_iid in self.checked else UNCHECKED
                    self.nodes[node_iid] = {"kind": "project", "id": obj["id"],
                                             "path": obj["path_with_namespace"],
                                             "proj": obj, "parent": iid}
                    if mark == CHECKED:
                        self.checked.add(node_iid)
                else:
                    self._insert_group(iid, obj, obj["id"], obj["full_path"],
                                       lazy=True)
            self.root.after(20, step)
        step()

    def _load_group_children(self, iid, gid):
        """后台拉取某个群组的子群组+项目，再回填到树里。完成后预取各子群组。"""
        try:
            subs, projects = self.client.group_children(gid)
        except Exception as e:
            self.root.after(0, lambda: self._show_group_error(iid, str(e)))
            return

        self.root.after(0, lambda: self._fill_children_anim(iid, subs, projects))

        # 懒加载兜底补到的数据也写进磁盘缓存，下次启动完整
        try:
            url = self.var_url.get().strip().rstrip("/")
            cache = load_tree_cache()
            tree = cache.setdefault(url, {"groups": [], "children": {}})
            tree["children"][str(gid)] = (
                [{"id": s["id"], "name": s["name"], "full_path": s["full_path"]}
                 for s in subs],
                [{"id": p["id"], "name": p["name"],
                  "path_with_namespace": p["path_with_namespace"],
                  "http_url_to_repo": p["http_url_to_repo"]}
                 for p in projects],
            )
            save_tree_cache(cache)
        except Exception:
            pass

        # 预取：把子群组的内容提前拉到缓存，用户点开时零等待
        for s in subs[:10]:   # 先预取前10个，避免瞬间打爆慢服务器
            sid = s["id"]
            if sid not in self._prefetch:
                self._prefetch[sid] = None
                threading.Thread(target=self._prefetch_children,
                                 args=(sid,), daemon=True).start()

    def _prefetch_children(self, gid):
        try:
            children = self.client.group_children(gid)
            self._prefetch[gid] = children
        except Exception:
            self._prefetch.pop(gid, None)

    def _show_group_error(self, iid, err):
        gid = self.nodes[iid]["id"]
        self.tree.delete(*self.tree.get_children(iid))
        self.tree.insert(iid, "end", text=f"   ⚠️ 加载失败：{err}", values=("", ""))
        # 放回占位节点，让箭头还在，下次展开可重试
        self.tree.insert(iid, "end", iid=f"ph-{gid}",
                         text="", values=("", ""))

    # ---------- 勾选（三态：☑全选 ▣半选 ☐未选，父子联动） ----------
    def _on_tree_click(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region != "tree":
            return
        iid = self.tree.identify_row(event.y)
        if not iid or iid not in self.nodes:
            return
        self._toggle(iid)

    def _toggle(self, iid):
        """切换某行勾选：向下传播到全部已加载子级，向上重算祖先半选状态。

        三态语义：未选→全选；全选→全不选；半选→全不选（点半选先清空）。
        """
        node = self.nodes.get(iid)
        if node["kind"] == "group":
            kids = [c for c in self.tree.get_children(iid) if c in self.nodes]
            half = (iid not in self.checked) and kids and \
                any(c in self.checked for c in kids)
            check = not (iid in self.checked or half)
        else:
            check = iid not in self.checked
        self._apply_subtree(iid, check)
        self._update_ancestors(node.get("parent"))
        self._refresh_sel_status()

    def _set_mark(self, iid, mark):
        """更新行首的勾选标记（替换 ☑/☐/▣ 任一）。"""
        text = self.tree.item(iid, "text")
        for m in (CHECKED, UNCHECKED, HALF):
            if m in text:
                self.tree.item(iid, text=text.replace(m, mark, 1))
                return

    def _apply_subtree(self, iid, check):
        """把勾选/取消应用到节点自身及其全部已加载后代。"""
        stack = [iid]
        while stack:
            cur = stack.pop()
            if cur not in self.nodes:
                continue  # 占位节点/加载提示行
            if check:
                self.checked.add(cur)
            else:
                self.checked.discard(cur)
            self._set_mark(cur, CHECKED if check else UNCHECKED)
            stack.extend(self.tree.get_children(cur))

    def _update_ancestors(self, parent_iid):
        """自下而上重算祖先：子级全勾→父全选☑；部分勾→父半选▣(不参与下载)；全不勾→☐。"""
        while parent_iid and parent_iid in self.nodes:
            kids = [c for c in self.tree.get_children(parent_iid) if c in self.nodes]
            n_checked = sum(1 for c in kids if c in self.checked)
            if kids and n_checked == len(kids):
                self.checked.add(parent_iid)
                self._set_mark(parent_iid, CHECKED)
            elif n_checked > 0:
                self.checked.discard(parent_iid)  # 半选的群组不整组下载
                self._set_mark(parent_iid, HALF)
            else:
                self.checked.discard(parent_iid)
                self._set_mark(parent_iid, UNCHECKED)
            parent_iid = self.nodes[parent_iid].get("parent")

    def _refresh_sel_status(self):
        self.set_status(f"已勾选 {len(self.checked)} 项（☑全选 ▣部分选中不整组下载）")

    # ---------- 下载实时监视 ----------
    def _start_monitor(self, out_root):
        """下载期间每 5 秒刷新进度页签。统计在后台线程，绝不阻塞界面。"""
        self._monitor_on = True
        self._monitor_busy = False
        self._monitor_prev = {"done": 0, "size": 0.0}
        self._monitor_root = out_root
        self.notebook.select(0)  # 自动切到进度页
        self._monitor_tick()

    def _stop_monitor(self):
        self._monitor_on = False

    def _monitor_tick(self):
        if not self._monitor_on:
            return
        # 上一轮统计还没跑完就跳过本轮，避免堆积
        if not self._monitor_busy:
            self._monitor_busy = True
            out = getattr(self, "_monitor_root", "")
            prev = dict(self._monitor_prev)

            def _compute():
                done = count_cloned(out)
                size_gb = disk_usage_gb(out)
                repos = transferring_repos()

                def _update():
                    # 回到主线程只做轻量 UI 更新
                    self._monitor_busy = False
                    if not self._monitor_on:
                        return
                    try:
                        self.lbl_prog_done.config(
                            text=f"已完成: {done} 个（较上次 +{max(0, done - prev['done'])}）")
                        self.lbl_prog_size.config(
                            text=f"磁盘占用: {size_gb:.2f} GB"
                                 f"（+{max(0.0, size_gb - prev['size']):.2f} GB）")
                        self.lbl_prog_run.config(text=f"正在克隆: {len(repos)} 个仓库")
                        self.txt_transferring.configure(state="normal")
                        self.txt_transferring.delete("1.0", "end")
                        if repos:
                            for r in repos:
                                self.txt_transferring.insert("end", "  ⬇️ " + r + "\n")
                        else:
                            self.txt_transferring.insert(
                                "end", "  （当前没有正在传输的仓库——可能在扫描群组或已全部完成）")
                        self.txt_transferring.configure(state="disabled")
                        self._monitor_prev = {"done": done, "size": size_gb}
                    except Exception:
                        pass
                self.root.after(0, _update)

            threading.Thread(target=_compute, daemon=True).start()
        self.root.after(5000, self._monitor_tick)

    def toggle_all(self):
        tops = [iid for iid in self.tree.get_children() if iid in self.nodes]
        if not tops:
            return
        check_all = not any(iid in self.checked for iid in tops)
        for iid in tops:
            self._apply_subtree(iid, check_all)
        self._refresh_sel_status()

    # ---------- 下载 ----------
    def on_download(self):
        if not self.checked:
            messagebox.showinfo("提示", "请先在树中勾选要下载的群组或项目")
            return
        out = self.var_out.get().strip()
        if not out:
            messagebox.showinfo("提示", "请先选择下载根目录")
            return
        os.makedirs(out, exist_ok=True)
        checked_iids = list(self.checked)
        self.btn_dl.config(state="disabled")
        self.btn_login.config(state="disabled")
        self.log("\n" + "=" * 60)
        self.log(f"准备下载 {len(checked_iids)} 个勾选项 -> {out}")
        self._start_monitor(out)
        threading.Thread(target=self._do_download, args=(checked_iids, out),
                         daemon=True).start()

    def _do_download(self, checked_iids, out_root):
        # 收集勾选范围内的所有项目（按 id 去重）
        seen, projects = set(), []
        for iid in checked_iids:
            node = self.nodes.get(iid)
            if not node:
                continue
            self.log(f"🔍 正在扫描群组: {node['path']} …")
            try:
                if node["kind"] == "group":
                    self._collect_group(self.client, node["id"], projects)
                else:
                    projects.append(node["proj"])
            except Exception as e:
                self.log(f"⚠️ 群组 {node.get('path','')} 加载失败：{e}")

        uniq = [p for p in projects if not (p["id"] in seen or seen.add(p["id"]))]
        jobs = [(os.path.join(out_root, *p["path_with_namespace"].split("/")), p)
                for p in uniq]
        total = len(jobs)
        self.log(f"共收集 {total} 个项目，开始克隆…\n")
        done = ok = skip = fail = 0
        state = {"done": 0, "total": total}

        # 心跳线程：克隆大仓库期间每 20 秒报一次进度，避免界面看起来卡死
        stop_beat = threading.Event()
        def heartbeat():
            while not stop_beat.wait(20):
                self.log(f"⏳ 进行中: 已完成 {state['done']}/{state['total']}，"
                         f"剩余 {state['total'] - state['done']} 个（大仓库克隆较慢，请耐心等待）")
        threading.Thread(target=heartbeat, daemon=True).start()

        with ThreadPoolExecutor(max_workers=5) as pool:
            futs = {pool.submit(self._clone_one, j): j for j in jobs}
            for fut in as_completed(futs):
                status, rel, detail = fut.result()
                done += 1
                state["done"] = done
                if status == "cloned":
                    ok += 1
                    self.log(f"[完成 {done}/{total}] {rel}")
                elif status == "skip":
                    skip += 1
                    self.log(f"[跳过 {done}/{total}] {rel}")
                else:
                    fail += 1
                    self.log(f"[失败 {done}/{total}] {rel}  ({detail})")
                self.root.after(0, lambda d=done, t=total: self.progress.config(value=min(d, t)))
        stop_beat.set()

        self.root.after(0, lambda: self.progress.config(value=total))
        self.log(f"\n✅ 完成：克隆 {ok}，跳过 {skip}，失败 {fail}，共 {total} 个项目")
        if fail:
            self.log("提示：失败通常是权限不足或项目为空，可展开对应群组核对。")
        self.root.after(0, lambda: [self._stop_monitor(),
                                    self.btn_dl.config(state="normal"),
                                    self.btn_login.config(state="normal"),
                                    self.btn_open.config(state="normal"),
                                    self.set_status("下载完成")])

    def _collect_group(self, client, gid, projects):
        """递归收集某群组下的所有项目（含嵌套子群组）。"""
        subs, direct = client.group_children(gid)
        projects.extend(direct)
        for s in subs:
            self._collect_group(client, s["id"], projects)

    def _clone_one(self, job):
        target, proj = job
        name = proj["name"]
        os.makedirs(target, exist_ok=True)
        if os.path.isdir(os.path.join(target, ".git")):
            return ("skip", os.path.relpath(target), name)
        if os.listdir(target):
            return ("skip", os.path.relpath(target), name)
        self.log(f"⬇️ 开始克隆 {name} ({proj['path_with_namespace']})")
        url = self.client.build_clone_url(proj)
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        host = urllib.parse.urlsplit(url).netloc.split("@")[-1]
        git_cfg = ["-c", "credential.helper=",
                   "-c", f"credential.http://{host}.allowUnsafeRemote=true",
                   "-c", f"credential.https://{host}.allowUnsafeRemote=true",
                   "-c", "http.lowSpeedLimit=1", "-c", "http.lowSpeedTime=10"]
        try:
            subprocess.run(["git"] + git_cfg + ["clone", "--", url, target],
                           check=True, capture_output=True, text=True,
                           timeout=1800, env=env)
            # 干净的 origin 指向公网地址（去掉内嵌 token 和 oauth2 用户名）
            clean = f"{self.client.base}{urllib.parse.urlsplit(url).path}"
            subprocess.run(["git", "-C", target, "remote", "set-url",
                            "origin", clean],
                           check=True, capture_output=True)
            return ("cloned", os.path.relpath(target), name)
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or e.stdout or "").strip().splitlines()
            return ("failed", os.path.relpath(target), detail[-1] if detail else "git error")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
