# Worktree 并行开发与 GitHub 同步

一个功能任务对应一个工作目录和分支。各 worktree 共享提交历史，文件修改互相独立；完成的代码通过 Pull Request 汇入 `main`。

## 创建工作区

在现有仓库运行，功能名和路径按实际任务调整：

```powershell
git fetch origin
git worktree add -b feature/example ../image-workbench-example origin/main
Set-Location ../image-workbench-example
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

Windows 使用较短的目录，避免任务记录和测试临时文件触及系统路径长度限制。
以 `git rev-parse --show-toplevel` 确认当前目录。每个工作区安装自己的依赖，并使用独立数据目录和端口：

```powershell
.\.venv\Scripts\python.exe -m image_workbench --data-root work/dev-data web --backend local --port 7871
```

其他工作区使用 7872、7873 等空闲端口。`local` 图片后端需要通过 `--image` 指定自己准备的测试图片才能演练生成；普通页面预览可不指定图片。
worktree 不隔离 GPU、外部模型服务或系统进程，真实执行器仍按既有队列与锁管理。

## 验证、推送与合并

```powershell
# 单功能检查；提交涉及公共服务的变更前运行完整检查。
.\.venv\Scripts\python.exe -X utf8 scripts/verify.py --module scripts
.\.venv\Scripts\python.exe -X utf8 scripts/verify.py

git status --short
git add <本次修改的文件>
git commit -m "Describe the completed change"
git push -u origin HEAD
```

在 GitHub 创建以 `main` 为目标的 Pull Request，确认差异与检查结果后合并。
其他工作区需要同步时，先保存当前改动，再执行 `git fetch origin` 和 `git merge origin/main`；有冲突时解决冲突并重新验证受影响功能。

业务数据、登录凭据、部署配置、模型权重和生成结果遵循现有 `.gitignore`，使用私下配置方式分发。
本仓库用 `.gitattributes` 保留原始字节，避免模板的 SHA-256 因换行转换失效。

GitHub 代码同步与服务器部署是独立操作。历史发布清单只用于核对对应冻结版本；新的代码合并后仍需按部署流程单独发布和验收。
