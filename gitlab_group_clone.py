#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
递归克隆 GitLab(极狐) 某个群组下的所有项目，本地目录按群组层级自动创建文件夹。

依赖：仅标准库 + 系统 git 命令（Windows 下请用 py 运行本脚本）。

用法示例：
  py gitlab_group_clone.py --token "你的token" --group "mygroup" --out ./download
  py gitlab_group_clone.py --token "你的token" --group 123 --out ./download      # group 也可用 ID
  py gitlab_group_clone.py --token "你的token" --group "parent/child" --list-only # 只预览不克隆
  py gitlab_group_clone.py --token "你的token" --group "parent/child" --auth ssh --out ./download
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_URL = "http://218.12.70.78:18081"
PER_PAGE = 100  # GitLab API 单页上限

# Windows 文件系统不允许的字符，替换为下划线
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str) -> str:
    return _INVALID.sub("_", name).strip(" .") or "_"


class GitLabAPI:
    """极简 GitLab API v4 客户端（标准库实现）。"""

    def __init__(self, base_url: str, token: str):
        self.api = base_url.rstrip("/") + "/api/v4"
        self.headers = {
            "PRIVATE-TOKEN": token,
            "User-Agent": "gitlab-group-clone",
        }

    def _get(self, path: str, params: dict = None) -> bytes:
        qs = urllib.parse.urlencode(params or {})
        full = f"{self.api}{path}" + (f"?{qs}" if qs else "")
        req = urllib.request.Request(full, headers=self.headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()

    def _paginate(self, path: str, params: dict = None) -> list:
        params = dict(params or {})
        params["per_page"] = PER_PAGE
        params["page"] = 1
        result = []
        while True:
            page = json.loads(self._get(path, params))
            result.extend(page)
            if len(page) < PER_PAGE:
                return result
            params["page"] += 1

    def get_group(self, group_id_or_path) -> dict:
        """group 参数可以是数字 ID，也可以是 URL 编码后的完整路径。"""
        ident = group_id_or_path if str(group_id_or_path).isdigit() \
            else urllib.parse.quote(str(group_id_or_path), safe="")
        return json.loads(self._get(f"/groups/{ident}"))

    def get_subgroups(self, group_id) -> list:
        return self._paginate(f"/groups/{group_id}/subgroups")

    def get_projects(self, group_id) -> list:
        # simple=true 只返回克隆所需的字段，体积小很多
        return self._paginate(f"/groups/{group_id}/projects", {"simple": "true"})


def build_clone_url(project: dict, auth: str, token: str) -> str:
    """构造用于 git clone 的 URL。

    http: 借用 oauth2 用户名把 token 内嵌进 URL，免交互。
    ssh : 直接用服务端提供的 ssh 地址（需本机已配置 SSH key）。
    """
    if auth == "ssh":
        return project["ssh_url_to_repo"]
    repo = project["http_url_to_repo"]
    scheme, rest = repo.split("://", 1)
    quoted = urllib.parse.quote(token, safe="")
    return f"{scheme}://oauth2:{quoted}@{rest}"


def walk_group(api: GitLabAPI, group: dict, out_root: str, jobs: list,
               stats: dict, auth: str, token: str):
    """递归：群组 -> 建文件夹 -> 收集直接子项目 -> 再进子群组。"""
    group_dir = os.path.join(out_root, sanitize(group["name"]))
    os.makedirs(group_dir, exist_ok=True)
    stats["groups"] += 1

    for p in api.get_projects(group["id"]):
        target = os.path.join(group_dir, sanitize(p["name"]))
        jobs.append({"target": target, "project": p})
        stats["projects"] += 1

    for sub in api.get_subgroups(group["id"]):
        walk_group(api, sub, group_dir, jobs, stats, auth, token)


def clone_one(job: dict, auth: str, token: str) -> tuple:
    """克隆单个项目。返回 (状态, 目标路径, 项目名)。"""
    target, proj = job["target"], job["project"]
    name = proj["name"]

    if os.path.isdir(os.path.join(target, ".git")):
        return ("skip", target, name)
    if os.path.isdir(target) and os.listdir(target):
        return ("skip-existing", target, name)  # 非空目录，避免覆盖

    url = build_clone_url(proj, auth, token)
    try:
        subprocess.run(
            ["git", "clone", "--", url, target],
            check=True, capture_output=True, text=True, timeout=1800,
        )
        # 克隆完成后把 remote 里的 token 清掉，避免凭据留在 .git/config
        clean = proj["http_url_to_repo"] if auth != "ssh" else proj["ssh_url_to_repo"]
        subprocess.run(
            ["git", "-C", target, "remote", "set-url", "origin", clean],
            check=True, capture_output=True,
        )
        return ("cloned", target, name)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip().splitlines()
        return ("failed", target, f"{name}: {detail[-1] if detail else 'git error'}")


def main():
    ap = argparse.ArgumentParser(description="递归克隆 GitLab 群组下的所有项目")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"GitLab 地址，默认 {DEFAULT_URL}")
    ap.add_argument("--token", default=os.environ.get("GITLAB_TOKEN", ""),
                    help="私有访问令牌（也可用环境变量 GITLAB_TOKEN）")
    ap.add_argument("--group", required=True,
                    help="目标群组的 ID 或完整路径，如 123 或 parent/child")
    ap.add_argument("--out", default="./gitlab_download", help="下载根目录，默认 ./gitlab_download")
    ap.add_argument("--auth", choices=["http", "ssh"], default="http",
                    help="克隆认证方式，默认 http（内嵌 token），ssh 需本机配好 key")
    ap.add_argument("--threads", type=int, default=4, help="并发克隆数，默认 4")
    ap.add_argument("--list-only", action="store_true", help="只列出群组树和项目，不克隆")
    args = ap.parse_args()

    # Windows 控制台中文显示
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not args.token:
        print("错误：缺少 token。请用 --token 或设置环境变量 GITLAB_TOKEN。\n"
              "获取方式：极狐 GitLab -> 用户头像 -> Preferences(偏好设置) -> Access Tokens，"
              "勾选 read_api / read_repository / api 后创建。")
        sys.exit(1)

    print(f"连接 {args.url} ...")
    api = GitLabAPI(args.url, args.token)
    try:
        root = api.get_group(args.group)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("错误：token 无效或无权限（401）。请检查 token 及 scopes。")
        elif e.code == 404:
            print("错误：群组不存在或无权访问（404）。")
        else:
            print(f"错误：HTTP {e.code}")
        sys.exit(1)
    except Exception as e:
        print(f"错误：无法连接服务器：{e}")
        sys.exit(1)

    stats = {"groups": 0, "projects": 0}
    jobs = []
    out_root = os.path.abspath(args.out)
    walk_group(api, root, out_root, jobs, stats, args.auth, args.token)

    print(f"\n群组：{root['full_path']}  ({root['name']})")
    print(f"共 {stats['groups']} 个群组（含自身），{stats['projects']} 个项目")

    if args.list_only:
        for j in jobs:
            print("  -", os.path.relpath(j["target"], out_root).replace("\\", "/"))
        return

    print(f"下载目录：{out_root}")
    print(f"开始克隆（并发 {args.threads}）...\n")

    ok = skip = fail = 0
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        futures = {pool.submit(clone_one, j, args.auth, args.token): j for j in jobs}
        for fut in as_completed(futures):
            status, target, name = fut.result()
            rel = os.path.relpath(target, out_root).replace("\\", "/")
            if status == "cloned":
                ok += 1
                print(f"  [克隆] {rel}")
            elif status == "skip":
                skip += 1
                print(f"  [已存在，跳过] {rel}")
            else:
                fail += 1
                print(f"  [失败] {name}")

    print(f"\n完成：克隆 {ok}，跳过 {skip}，失败 {fail}，共 {len(jobs)} 个项目")
    if fail:
        print("有项目失败，通常是权限不足或项目为空，请检查上面 [失败] 项。")


if __name__ == "__main__":
    main()
