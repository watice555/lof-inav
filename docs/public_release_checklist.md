# Public Release Checklist

本清单用于把当前私有工作仓库整理成适合公开的 clean public repo。当前仓库可以继续保留本地缓存、实验材料和完整历史；公开仓库只发布源码、配置、文档和必要元数据。

## 推荐许可证

建议优先使用 `MIT License`。

原因：

- 简单、短、宽松，适合个人学习项目和工具型项目。
- 允许他人使用、复制、修改、分发和商用。
- 要求保留版权和许可证声明。
- 自带免责声明，明确按现状提供，不承诺担保。

如果你特别在意专利授权条款，可以改用 `Apache License 2.0`。它也很宽松，但文本更长，并包含明确的专利授权和专利终止条款。这个项目目前没有明显专利诉求，MIT 更符合“个人工具开源”的维护成本。

## 当前仓库检查

- [x] README 已声明项目仅供个人学习、研究和技术验证。
- [x] README 已说明不保证准确、不构成投资建议。
- [x] `data/` 已整体加入 `.gitignore`。
- [x] 原始公告 PDF、抽取文本、HTML、SQLite 缓存已从 git 索引移除。
- [x] 全量 LOF 清单已移动到 `docs/lof_universe_gap.csv` 并继续跟踪。
- [x] 添加根目录 `LICENSE` 文件。
- [x] README 增加许可证说明，例如 `License: MIT`。
- [ ] 公开前确认 `git status --short` 为空。

## Clean Public Repo 步骤

在当前私有仓库之外创建公开目录：

```powershell
cd D:\Projects_local
git clone --no-local D:\Projects_local\LOF_iNAV LOF_iNAV_public
cd LOF_iNAV_public
Remove-Item -Recurse -Force .git
git init
```

确认 `data/` 仍被忽略：

```powershell
git status --short --ignored
```

添加许可证后创建公开初始提交：

```powershell
git add .
git commit -m "Initial public release"
git branch -M main
git remote add origin https://github.com/<your-user>/<repo-name>.git
git push -u origin main
```

## 公开前验证

检查仓库中不会包含 `data/`：

```powershell
git ls-files data
```

检查大文件：

```powershell
$files = git ls-files | ForEach-Object { Get-Item -LiteralPath $_ }
$files | Sort-Object Length -Descending | Select-Object -First 20 @{Name='Path';Expression={$_.FullName.Substring((Get-Location).Path.Length + 1)}},Length
```

检查明显密钥：

```powershell
rg -n --hidden "(?i)(api[_-]?key|secret|password|passwd|access[_-]?token|bearer|authorization|BEGIN (RSA|OPENSSH|PRIVATE) KEY)"
```

`app/sources.py` 中东方财富接口的 `token` 参数是公开行情接口固定参数，不应是个人凭证；公开前可以再人工确认一次。

## 后续同步方式

继续在私有仓库 `LOF_iNAV` 开发，然后把干净文件同步到公开仓库：

```powershell
robocopy D:\Projects_local\LOF_iNAV D:\Projects_local\LOF_iNAV_public /MIR /XD .git data __pycache__ experimental /XF *.pyc
cd D:\Projects_local\LOF_iNAV_public
git status
git add -A
git commit -m "your change"
git push
```

注意：`robocopy /MIR` 会删除目标仓库中源仓库没有的文件；命令排除了 `.git`，所以公开仓库历史会保留。
