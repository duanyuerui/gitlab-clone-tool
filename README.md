# 极狐 GitLab 群组批量克隆工具

按群组层级递归克隆 GitLab（极狐）全部项目的桌面工具。树形勾选要下载的群组/项目，一键并发克隆到本地，目录结构镜像服务器上的群组层级。

## 功能特性

- **图形界面**（Tkinter，无需额外依赖，仅需 Python 3.8+ 和系统 git）
- **三种登录方式**：Personal Access Token 粘贴 / 浏览器 OAuth 授权 / 用户名密码（需实例支持）
- **树形勾选**：☑ 全选（含所有子级）/ ▣ 半选（只下勾选的子项）/ ☐ 未选，父子状态联动
- **全树磁盘缓存**：首次拉取后存本地，再次启动 <0.1 秒整棵树秒开；后台增量合并刷新，展开/勾选状态不丢
- **懒加载动画**：缓存未覆盖的节点展开时显示加载动画，数据级联刷入，拉到即写缓存
- **并发克隆**：默认 5 线程，自动改写内网地址为可访问地址、免交互认证、克隆后清除 remote 中的凭据
- **实时进度**：下载进度页签（完成数/磁盘占用/正在传输的仓库列表）+ 心跳日志，长时间克隆不再"假死"
- **断点续传**：已克隆的项目自动跳过，中断后重跑即可续传
- **失败重试**：全树拉取自动重试 3 轮，网络抖动不丢群组

## 使用方法

```bash
py gitlab_clone_gui.py        # Windows（用 py 启动器）
python3 gitlab_clone_gui.py   # Linux/macOS
```

1. 填写服务器地址（如 `http://your-gitlab:port`）
2. 点「粘贴Token」输入 Personal Access Token（推荐；账号开 2FA 时唯一可用方式），或用其他登录方式
3. 等待群组树加载（首次约 30-60 秒，之后秒开）
4. 展开树，勾选要下载的群组或具体项目（勾选群组 = 递归全选其下所有项目）
5. 选择下载根目录，点「开始下载勾选项」

另有命令行版本 `gitlab_group_clone.py`，适合脚本化/定时任务场景。

## 命令行版用法

```bash
py gitlab_group_clone.py --token "glpat-xxx" --group "group-path" --out ./download
py gitlab_group_clone.py --token "glpat-xxx" --group 123 --list-only   # 只预览不克隆
```

## 安全说明

- Token 仅保存在本机 `~/.gitlab_clone_gui.json`，不入库、不随项目分发
- 克隆完成后自动把每个仓库 remote 中的凭据清除
- 群组树缓存（`~/.gitlab_tree_cache.json`）只含结构数据，不含凭据

## 已知限制

- 明文 HTTP 服务器：程序自动附加 `allowUnsafeRemote` 配置放行 git 的安全拦截
- 服务器返回的 `http_url_to_repo` 若为内网地址，程序自动改写为实际访问地址克隆
- 2FA 账号无法使用"用户名密码"登录（OAuth password grant 会被拒），请用 Token 或浏览器授权
