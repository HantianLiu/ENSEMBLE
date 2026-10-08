# 安装与配置 / Installation and configuration

> v0.7.5 软件发行版（v0.7.1 文献写作规则）：本目录安装的主命令是 `ensemble`，旧版本改用
> `ensemble-old`。下文从 v0.7.0 继承的示例中如需运行本版本，请把命令读作
> `ensemble`，并使用本目录的独立虚拟环境。`ensemble-v071` 仅保留为兼容别名。
> 新文献报告会议会额外询问学术主笔的模型和推理强度；旧会议请用 `ensemble-old`
> 恢复。

初始化菜单中的「快速文献调研」是独立试行流程：不指定 Chair，须指定一名学术主笔、
Research Desk 和至少两个不同基础模型的智库长审阅者。主笔先拟任务书供人类确认；
在题目澄清对话中，主笔可按需做最多 4 条轻量检索。第 1 步形成候选任务书后，
以及第 2 步制定模块执行单前，主笔各可安排一次最多 8 条检索式的广度探索。
这些结果标为规划线索，不能替代第 3 步对报告论断的正式证据核查；检索结果会落盘，
恢复会议不会重复查询。已冻结模块执行单的旧会议不会补插新的规划检索。
此模式不会自动改动或升级已经运行的普通会议。
智库长现在采用一席主审与故障备用：平时只有主审参加科学审阅和智库长表决，
备用模型仅在主审输出耗尽、供应商调用失败或结构化响应最终失败后接席。
接席原因与失败记录保存在会议审计层，已冻结的旧票和旧稿不改写。
如果 Research Desk 的某条请求收到供应商明确的内容风险拒绝，终端会在并行批次收尾后
提示人类选择备用模型，或保持会议暂停。已落盘的核查不重做；选定备用模型后只重试未落盘项，
并记录未来生效的模型替换。供应商错误详情保存在会议目录的
`audit_private/provider_failures/`，不会写入公开证据包，也不会被当作文献结论。

## 环境依赖 / Requirements

- Python **3.12+** 和 `pip`。Python 包依赖由 `pyproject.toml` 自动安装：`httpx`、`pydantic`、`reportlab`、`pypdf`、`matplotlib`。运行主流程不需要 Node.js、Pandoc 或 LaTeX；PDF 中的常用 Markdown 数学公式由 Matplotlib 渲染。若要自行编译导出的 `.tex`，则另装支持 `ctexart`、`amsmath`、`amssymb` 的 TeX 引擎。
- PDF 默认使用随程序打包的 HarmonyOS Sans SC：Regular 用于正文，Medium 用于节标题，Bold 用于章节标题和目录。原版字体来自[华为官方设计资源](https://developer.huawei.com/consumer/cn/design/resource-V1/)；字体受华为自己的协议约束，不受本项目许可证覆盖。完整许可与版权声明随字体放在 `project_ensemble/assets/fonts/LICENSE.txt`，另见根目录 `THIRD_PARTY_NOTICES.md`。如需覆盖，使用 `ENSEMBLE_CJK_FONT` 指定正文 TTF/TTC，使用 `ENSEMBLE_CJK_HEADING_FONT` 指定标题 TTF。
- 能访问所选模型供应商的网络连接。启用 Research Desk 时，还需要访问 OpenAlex；Tavily 仅在显式启用时使用。
- 可选：Codex CLI（ChatGPT 订阅登录方式）、Claude Code CLI（登录或 API 密钥方式）、Slurm `sbatch`（只在启用调度器邮件时需要）。缺少这些可选工具不影响其他供应商。
- Linux 为当前验证平台；macOS 尚未完成端到端验证。Windows 原生运行仍受 `fcntl`／`termios` 等 Unix 依赖限制，现阶段请用 WSL2 安装 Linux 版；独立 Windows 发行版尚未发布。

## 安装 / Install

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
ensemble
```

Windows 用户在 WSL2 的 Linux shell 中运行上述命令。以下 PowerShell 命令仅展示创建 Python 虚拟环境的形式，**目前不能保证原生 Windows 完整会议流程可运行**：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
ensemble
```

开发测试另用 `python -m pip install -e '.[dev]'`。在任意工作目录启动 `ensemble`，首页选择“设置 → 模型供应商”；设置结束后返回首页召开会议。新会议文件保存在**启动命令所在的当前工作目录**，不是配置目录。会议在初始化时询问标题，并将会议 ID、标题、类型和工作区路径记入用户全局索引，便于跨目录恢复。

## 设置界面 / Settings wizard

首次运行 `ensemble` 会先选择中文或 English 界面；以后在“设置 → 界面语言”更改。该选择保存在用户目录的 `interface.toml`，只控制界面菜单与进度标签，不改变会议提示词、证据、正文或报告输出语言。正文语言在会议初始化时单独选择。初始化页会介绍会议形式、流程和模型分工。Technician 是可选的会议运行排障模型；如启用，必要的任务和证据片段可能发送到所选模型供应商，初始化界面会提示这一风险。

议事会议现在也可以在正式建会前选择“与主席对话形成任务委托”。这段会前对话只澄清议题、可用材料、硬约束、偏好、交付文书和人类保留的裁定，不提前提案或投票；人类确认的完整版本写入会议原始任务。直接输入任务的旧入口仍保留，已运行会议不受影响。

首页“设置”提供五项：界面语言、界面颜色与表格宽度、模型供应商、联网搜索后端、证据包缓存最大复用时长。OpenAlex 是学术搜索主后端，可匿名使用、引用环境变量或已有密钥文件，也可输入密钥保存到用户目录；Tavily 可选作为网页检索补充，以相同三种方式配置密钥。当前版本不把 Tavily 当作 OpenAlex 的完全替代。供应商向导依次询问：

Research Desk 启用时，新会议初始化另会询问 OpenAlex 的 **HTTP 429 处理策略**：默认先查询官方额度接口。确认当日余额不足时，简易文献调研会在后台继续已在执行、无需新检索的工作，同时请人类选择是否让 Tavily 接手；拒绝则完成这些工作后暂停，不等额度重置。余额未证实耗尽的 429 只做有界退避重试。人类也可预先授权 Tavily 接手，并在原文补读后尝试 OpenAlex 补检；未补成会记录学术覆盖不足。Ctrl+R 可为尚未启动的请求修改策略，包括旧会议，而不改写冻结的初始化配置。分阶段的简易文献调研与代表模型队列中，模型更换和扩大并行度在退出菜单后立即作用于未启动子任务；缩小并行度不会取消在途请求，而会停止补位，直到在途数降到新上限。OpenAlex 一次只发送一个 HTTP 请求；候选返回后即可发下一次检索，不必等待前一次的原文读取结束。连接故障经重试仍未恢复时，若已配置 Tavily 则自动接手；网页及法规检索仍优先使用 Tavily。

若 OpenAlex 的 429 响应缺少余额响应头，程序会使用相同凭据查询官方只读 `/rate-limit` 接口，区分当日额度不足和仍有余额的短时限流；诊断结果短时缓存，避免并发问题反复查询。随附配置把 OpenAlex 的同时在途 HTTP 请求上限设为 **1 个**，两次检索起始时间最少间隔 **1 秒**；这两个数值是客户端输入控制参数，不是供应商公布的限制。

1. 接口格式：OpenAI 兼容、Gemini 原生、Codex CLI、Claude Code CLI。
2. 自定义显示名称与稳定的英文代号；后者作为会议记录里的 `provider_id`，创建后不可覆盖同名旧配置。
3. API 地址（适用时）和身份验证方式。API 密钥可直接引用环境变量、引用已有外部 `NAME=value` 文件，或隐藏输入后保存到用户专用目录。环境变量优先于外部文件；文件只作为文本解析，**不会 `source` 或执行**。
4. Claude Code 的模型 ID 由用户显式列出；Codex 从登录账户查询模型列表。CLI 方式需先在本机完成相应登录。Claude Code 的 API 密钥模式会按官方 CLI 行为使用 API 计费，不能与订阅登录混为一谈。

设置路径：Linux/macOS 为 `${XDG_CONFIG_HOME:-~/.config}/ensemble/`，Windows 为 `%APPDATA%\ensemble\`。该目录包含 `ensemble.toml`、`model_config.toml`、`interface.toml`、`appearance.toml`、`meetings.json`，以及按需创建的 `secrets/`。密钥文件在 Unix 上使用仅当前用户可读的权限。不要把这些用户文件提交到 GitHub；项目仓库内的 `examples/*.toml` 不含密钥。

仍可使用原有方式：`ENSEMBLE_CONFIG=/absolute/path/ensemble.toml` 或 `ensemble --config /absolute/path/ensemble.toml`；模型配置中的 `api_key_env`、`api_key_file` 继续有效。查找顺序为显式 `--config`、`ENSEMBLE_CONFIG`、用户级配置、源码检出目录里的配置。会议创建时冻结其配置，之后修改设置**不会改变正在运行或已完成会议**。

验证安装：

```bash
ensemble doctor
ensemble discover-models
ensemble
```

首次尚无任何配置时，`ensemble` 仍可打开首页和设置。`ensemble doctor` 则需要先配置至少一个模型或显式提供配置文件。

## English quick reference

On first launch, select Chinese or English for the interface. Change this later in **Settings → Interface language**. This does not change meeting prompts or report language, which are separate setup choices. The new-meeting guide explains workflow types and model roles. An optional Technician may send the minimum task and evidence excerpts needed for technical recovery to its chosen model provider; the risk is disclosed during setup.

Install with Python 3.12+ using `pip install -e .`, then run `ensemble` from the directory where new meeting workspaces should be created. Choose **Settings** to configure appearance, model providers, OpenAlex/Tavily search backends, and evidence-cache freshness. Credentials may come from an environment variable, an existing external assignment file, or an interactive hidden entry stored outside the repository. User configuration and the cross-workspace meeting index live under your platform's user config directory. `--config` and `ENSEMBLE_CONFIG` remain supported and take precedence. Optional Codex and Claude Code CLI integrations require their respective CLI installation and authentication; ordinary HTTP providers do not. The live footer counts unique cited source records (literature and web pages); it is not a count of full-text pages actually read by a model.
