# Public Repo Workflow

本文件记录当前项目的日常开源发布流程。

## 仓库路径

私有工作仓库：

```powershell
D:\Projects_local\LOF_iNAV
```

公开 clean 仓库：

```powershell
D:\Projects_local\LOF_iNAV_public
```

公开 GitHub 仓库：

```text
https://github.com/watice555/lof-inav
```

私有仓库可以保留本地缓存、原始公告材料、实验历史和完整开发历史。公开仓库只发布源码、配置、文档和必要元数据。

## 日常开发流程

先在私有仓库修改和验证：

```powershell
cd D:\Projects_local\LOF_iNAV
git status
```

提交私有仓库：

```powershell
git add <changed-files>
git commit -m "your commit message"
```

示例：

```powershell
git add README.md docs\public_repo_workflow.md
git commit -m "docs: update public repo workflow"
```

## 同步到公开仓库

从私有仓库同步干净文件到公开仓库：

```powershell
robocopy D:\Projects_local\LOF_iNAV D:\Projects_local\LOF_iNAV_public /MIR /XD .git data __pycache__ experimental /XF *.pyc
```

说明：

- `/MIR` 会让公开目录和私有目录保持一致，并删除公开目录中私有目录没有的文件。
- `/XD .git data __pycache__ experimental` 会保留公开仓库自己的 git 历史，并排除本地缓存和实验目录。
- `/XF *.pyc` 排除 Python 字节码文件。

## 提交并推送公开仓库

```powershell
cd D:\Projects_local\LOF_iNAV_public
git status
git add -A
git commit -m "your commit message"
git push
```

如果同步后没有变化，`git commit` 会提示没有内容可提交，可以直接跳过。

## 发布前快速检查

确认 `data/` 没有进入公开仓库索引：

```powershell
cd D:\Projects_local\LOF_iNAV_public
git ls-files data
```

正常情况下这条命令没有输出。

检查明显密钥痕迹：

```powershell
rg -n --hidden -g '!.git/' "(?i)(api[_-]?key|secret|password|passwd|access[_-]?token|bearer|authorization|BEGIN (RSA|OPENSSH|PRIVATE) KEY)"
```

`app/sources.py` 中东方财富接口的 `token` 参数是公开行情接口固定参数，不是个人凭证。

## 注意事项

- 优先在私有仓库改代码，再同步到公开仓库。
- 尽量不要直接在 GitHub 网页上修改公开仓库，否则需要手动同步回私有仓库。
- 公开仓库的历史应保持干净，不要把 `data/`、原始公告 PDF、抽取文本、HTML 或 SQLite 缓存提交进去。
- 如果公开仓库出现误提交，先不要继续推送，回到本地检查 `git status` 和 `git log` 后再处理。
