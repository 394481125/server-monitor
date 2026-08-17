# GitHub 更新与发布

## 日常更新

在已经包含 `.git` 和 `origin` 的源码目录执行：

```bash
bash scripts/update_github.sh "说明本次修改"
```

脚本会先同步远端分支，再运行 Python 测试、JavaScript 语法检查、Node 前端逻辑测试，并在本机有 Chrome 时运行 E2E；随后检查暂存差异、提交并推送。仅文档修改且明确接受跳过测试时：

```bash
bash scripts/update_github.sh "更新文档" --skip-tests
```

发生 rebase 冲突时手工解决并重新运行测试，不要使用强制推送。

## 首次发布到新仓库

`publish_github.sh` 只用于不含 `.git` 的发布目录：

```bash
bash scripts/publish_github.sh git@github.com:OWNER/REPOSITORY.git "首次发布"
```

脚本拒绝非 GitHub 地址和已有 Git 仓库。远端已有 `main` 时会先基于远端提交创建后续提交，不执行 force push。

## 构建发布包

```bash
bash scripts/build_release.sh vX.Y.Z
(cd dist && sha256sum -c SHA256SUMS)
```

输出：

- `server-monitor-github-vX.Y.Z/`：源码、Python/JavaScript 测试、CI 和文档。
- `server-monitor-deploy-vX.Y.Z/`：生产运行所需的轻量文件。
- 对应两个 `.tar.gz` 和 `SHA256SUMS`。

构建脚本不会覆盖已有版本目录。`.env`、`data/`、`.venv/`、数据库、主密钥和真实凭据不会进入发布包。

## 标签发布

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

推送前应确认：

```bash
git status --short
.venv/bin/python -m pytest -q
node --test tests_js/*.test.js
.venv/bin/python scripts/e2e_acceptance.py
git diff --check
```

CI 的 `test` Job 会运行后端测试、前端逻辑测试、真实 Chrome E2E、依赖检查和 compileall；`container` Job 会构建 Docker 镜像。
