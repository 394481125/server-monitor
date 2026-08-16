# GitHub 上传、更新与发布

本文对应仓库 `git@github.com:394481125/server-monitor.git`。首次上传和日常更新必须区分：已经有 `.git` 和 `origin` 的目录不能再次执行首次发布脚本。

## 首次上传

在 GitHub 源码包目录执行：

```bash
cd /home/qq394481125/app/server_monitor/dist/server-monitor-github-v1.0.0
bash scripts/publish_github.sh git@github.com:394481125/server-monitor.git "发布 v1.3.5"
```

脚本会初始化 `main`。如果 GitHub 仓库已经有 README 或其他提交，脚本会先 fetch 远程 `main`，再把当前发布目录作为后续提交推送，不使用强制推送。它只接受 GitHub URL，并会拒绝已经是 Git 仓库的目录。公开仓库发布前请自行选择并加入 `LICENSE`。

发布目录即使位于另一个源码仓库的 `dist/` 下面，脚本也只使用发布目录自己的 `.git`，不会向上误用父目录仓库。第一次必须运行 `publish_github.sh`；成功后该目录才可以运行 `update_github.sh`。

## 日常更新

```bash
cd /home/qq394481125/app/server_monitor/dist/server-monitor-github-v1.0.0
bash scripts/update_github.sh "修复扫描超时提示"
```

脚本会运行 pytest 和 JavaScript 语法检查、`git add -A`、检查差异、提交并推送当前分支。只改文档且暂时没有测试环境时才使用：

```bash
bash scripts/update_github.sh "更新部署说明" --skip-tests
```

多人协作或 GitHub 网页改过代码时，`update_github.sh` 会先自动 fetch/rebase；也可以手动先同步：

```bash
git pull --rebase origin main
bash scripts/update_github.sh "合并后更新"
```

发生冲突时手工解决、运行测试后再提交；不要强制推送覆盖他人提交。没有改动时脚本会输出 `No changes to commit.`。

## 发布包

在开发源码根目录使用新版本号：

```bash
cd /home/qq394481125/app/server_monitor
bash scripts/build_release.sh v1.3.5
(cd dist && sha256sum -c SHA256SUMS)
```

输出：

- `server-monitor-github-v1.3.5/`：源码、测试、CI 和完整文档。
- `server-monitor-deploy-v1.3.5/`：不含测试的轻量部署包。
- 两个压缩包和 `SHA256SUMS`。

版本目录不会覆盖，重复版本必须换版本号。`.env`、`data/`、`.venv/`、数据库、主密钥和真实凭据不会进入包。

## 标签和 GitHub Actions

`.github/workflows/ci.yml` 在 push/PR 上运行 pytest、pip check、JavaScript 检查和 compileall。推送版本标签会触发镜像/Release 工作流：

```bash
git tag v1.3.5
git push origin v1.3.5
```

发布前先确认工作树和提交内容：

```bash
git status
git remote -v
git branch --show-current
git diff --cached --check
git log --oneline -10
```

## 常见问题

### `Permission denied (publickey)`

```bash
ssh -T git@github.com
git remote set-url origin https://github.com/394481125/server-monitor.git
```

### `fetch first` 或 `non-fast-forward`

```bash
git pull --rebase origin main
bash scripts/update_github.sh "解决远端提交后的更新"
```

首次发布不要手动 `git init && git push`。请使用发布目录中的 `scripts/publish_github.sh`，它会处理远程已有初始提交的情况。

### `not a git repository`

说明当前是部署包或错误路径。进入含 `.git/` 的 GitHub 源码目录；部署包不要自行初始化第二个仓库。

### 检查敏感文件

```bash
git status --short
git ls-files | rg '(^data/|\.env$|master\.key|\.sqlite3$|id_rsa|\.pem$)'
```

任何敏感文件出现在输出中，都应在首次提交前移除并轮换凭据。
