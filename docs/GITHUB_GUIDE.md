# GitHub 上传与更新教程

本文适用于当前仓库：

- GitHub 地址：`git@github.com:394481125/server-monitor.git`
- 默认分支：`main`
- 当前本地源码仓库：你实际存放源码的目录

## 日常更新只记这两条

先进入已经发布到 GitHub 的源码目录：

```bash
cd /path/to/server-monitor-github
```

修改代码后执行：

```bash
bash scripts/update_github.sh "说明本次修改内容"
```

例如：

```bash
bash scripts/update_github.sh "修复主机告警显示"
```

该脚本会自动完成：

1. 检查当前 Git 仓库、分支和 `origin`。
2. 在环境可用时运行 pytest 和前端 JavaScript 语法检查。
3. 执行 `git add -A`。
4. 创建 Git commit。
5. 推送到 GitHub 当前分支。

如果当前目录没有安装测试依赖，可以临时跳过本地测试：

```bash
bash scripts/update_github.sh "更新部署文档" --skip-tests
```

跳过本地测试不影响 GitHub Actions；代码推送后，GitHub CI 仍会自动检查项目。

## 第一次上传新仓库

只有创建全新的 GitHub 仓库时才使用：

```bash
bash scripts/publish_github.sh git@github.com:<用户名>/<仓库名>.git
```

当前 `server-monitor` 已经完成首次上传，不要再次运行首次发布脚本。

## 修改代码前同步 GitHub

如果其他电脑、其他开发者或 GitHub 网页修改过代码，先同步：

```bash
cd /path/to/server-monitor-github
git pull --ff-only origin main
```

然后修改代码，再运行：

```bash
bash scripts/update_github.sh "本次修改说明"
```

如果只有你在这台电脑上维护仓库，通常直接运行更新脚本即可。

## 查看当前状态

查看哪些文件发生变化：

```bash
git status
```

查看最近提交：

```bash
git log --oneline -10
```

查看 GitHub 远程地址：

```bash
git remote -v
```

查看当前分支：

```bash
git branch --show-current
```

## 发布新版本

普通修改只需要运行 `update_github.sh`。准备发布正式版本时，再创建版本标签：

```bash
git tag v1.0.1
git push origin v1.0.1
```

推送 `v1.0.1` 这类标签后，GitHub Actions 会：

1. 创建 GitHub Release。
2. 生成源码包和部署包。
3. 生成 SHA-256 校验文件。
4. 发布 GHCR Docker 镜像。

版本号建议：

- 修复错误：`v1.0.1`
- 增加兼容功能：`v1.1.0`
- 存在不兼容改动：`v2.0.0`

## 撤销错误提交

先查找提交编号：

```bash
git log --oneline -10
```

用新的撤销提交恢复：

```bash
git revert <提交编号>
bash scripts/update_github.sh "撤销有问题的修改"
```

不要随意执行 `git reset --hard` 或强制推送，它们可能导致代码丢失。

## 常见错误

### Permission denied (publickey)

说明 GitHub SSH 密钥没有配置好。测试连接：

```bash
ssh -T git@github.com
```

也可以把远程地址切换成 HTTPS：

```bash
git remote set-url origin https://github.com/394481125/server-monitor.git
```

### non-fast-forward

说明 GitHub 上有本地没有的提交。先执行：

```bash
git pull --rebase origin main
```

解决冲突并确认测试通过后，再运行更新脚本。

### No changes to commit

说明代码没有变化，或者修改的文件被 `.gitignore` 忽略。这不是错误。

### Git 用户信息未配置

执行一次：

```bash
git config --global user.name "你的 GitHub 用户名"
git config --global user.email "你的 GitHub 邮箱"
```

## 禁止上传的内容

以下内容已经通过 `.gitignore` 排除，不要使用强制参数上传：

- `.env`
- `data/`
- `master.key`
- SQLite 数据库及备份
- SSH 私钥
- `.venv/`
- `__pycache__/`
- 真实服务器密码、sudo 密码和令牌

上传前可以执行：

```bash
git status
git diff --cached
```

确认没有敏感信息后再推送。

## 开源许可证

公开仓库如果希望其他人合法使用、修改和再发布，需要在仓库根目录添加 `LICENSE`。常见选择：

- MIT：限制少，适合希望广泛使用的项目。
- Apache-2.0：包含明确的专利授权条款。
- GPL-3.0：要求修改和衍生版本继续开源。

许可证属于项目所有者的法律选择，不应随意自动生成。
