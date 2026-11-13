# XAU Next

Windows 本机运行的 XAUUSD 行情分析、人工交易辅助与复盘工具。MT5 只读，交易由用户手动执行。

## 分享范围

本仓库包含后端、网页、只读桥、接口契约、合成样例、测试、构建与备份工具。源码以本机安装包对应的集成源码为基础整理，接口契约版本为 1.14.9，包含 Anthropic Messages 支持。

仓库不分发真实账户、持仓、交易、历史回答、数据库、附件、密钥、会话令牌、机器部署信息及个人开发历史。测试中的账户和令牌均是用于验证的合成值。程序版本与个人运行数据分别维护。

## 开发环境

- Windows，Python 3.12。
- Node.js；pnpm 版本以 `package.json` 的 `packageManager` 为准。
- uv 0.13.0；Python、MT5 桥和网页依赖由各自锁文件固定。

在 PowerShell 中进入仓库目录后安装开发依赖：

```powershell
py -3.12 -m pip install uv==0.13.0
uv sync --locked
uv sync --locked --project apps/mt5_bridge
npx --yes pnpm@10.28.2 install --frozen-lockfile
```

检查接口、架构和网页构建：

```powershell
.\.venv\Scripts\python.exe tooling/contracts/generate.py --check
.\.venv\Scripts\python.exe tooling/checks/architecture.py
npx --yes pnpm@10.28.2 build
```

## 在另一台电脑运行

推荐先构建安装候选，再在新的空数据目录安装。构建需要 Python、Node.js、pnpm 和 uv；安装后的日常运行不需要 Node.js。

Git 克隆目录和 GitHub Download ZIP 解压目录均可构建。ZIP 不带 Git 历史，构建清单会如实保留提交身份未知，并用文件哈希记录产物完整性。

```powershell
.\.venv\Scripts\python.exe tooling/package/build_candidate.py --destination ..\XAU-Next-Package
```

进入生成的安装包目录，运行 `Setup.cmd`，然后运行 `Start.cmd`。默认数据目录位于当前用户的 `%LOCALAPPDATA%\XAU-Next\data`。首次安装可能需要联网下载锁定依赖。

新机器需自行安装并登录 MT5，在设置中选择终端。模型 API 连接及密钥由使用者在本机配置；源码不会带入原机器的账号和密钥。详细操作见 `docs/OPERATIONS.md`。

## 维护与验证

业务规则见 `REBUILD_SPEC.md`，架构见 `docs/ARCHITECTURE.md`，开发约定见 `AGENTS.md`。分享版本的验证范围见 `docs/VALIDATION.md`。本仓库创建不代表预测准确率或交易效果得到验证。

提交前检查文件清单，继续排除 `.env`、数据库、附件、日志和测试产生的原始记录。不要把本机运行数据目录放进源码仓库。历史运行记录保留在本机，不作为分享材料。
