# Codex Register Fix

基于 [codex-manager](https://github.com/cnlimiter/codex-manager) 二次开发，修复了原项目因 OpenAI 授权流程变更导致的注册失败问题。

> **本项目基于 [cnlimiter/codex-manager](https://github.com/cnlimiter/codex-manager) 进行二次开发，原项目采用 MIT 协议开源，感谢原作者的贡献。**

---

## 免责声明

> **本项目仅供学习交流和技术研究使用，严禁用于任何商业用途或违法违规行为。**
>
> 1. 本项目不提供任何形式的担保，使用本项目产生的一切后果由使用者自行承担
> 2. 使用者应严格遵守 [OpenAI 使用条款](https://openai.com/policies/terms-of-use) 及所在地区相关法律法规
> 3. 本项目不鼓励、不支持任何形式的滥用行为，包括但不限于批量注册、刷号、倒卖账号等
> 4. 如本项目侵犯了任何第三方的合法权益，请及时联系，将在确认后第一时间删除
> 5. 本项目作者不对任何因使用或滥用本项目而导致的直接或间接损失负责
>
> **下载或使用本项目即表示您已阅读并同意以上声明。如不同意，请立即删除本项目。**

---

## 主要修复内容

原项目因 OpenAI 更新了 OAuth 授权流程而导致注册后无法获取 Workspace / Token，具体修复如下：

### 架构级修复

- **分离注册与 OAuth 登录流程**：原项目试图在注册 session 的 cookie 中直接提取 workspace 信息，但 OpenAI 的 workspace 数据只有在完整的 OAuth 登录流程中才会出现。新增 `_perform_oauth_login()` 方法，在注册完成后执行独立的 7 步 OAuth 登录流程获取 Token

### 协议级修复

- **Sentinel PoW Token 生成**：移植完整的 `SentinelTokenGenerator`，支持 proof-of-work 计算，通过 OpenAI 的 Sentinel 反自动化检测
- **Datadog APM Trace Headers**：添加 `traceparent`、`x-datadog-origin` 等 trace headers，模拟真实浏览器的 RUM SDK 行为
- **浏览器指纹升级**：`impersonate` 从 `chrome`（通用）升级为 `chrome131`（具体版本），更贴合真实浏览器 TLS 指纹
- **请求格式修正**：将 `data=json.dumps(...)` 统一替换为 `json={...}`，使 Content-Type 与 body 编码一致

### 其他修复

- **Starlette 兼容性**：修复 `TemplateResponse` API 在 Starlette 1.0 下的参数变更
- **用户信息生成**：补全姓名生成逻辑（名 + 姓），修复 `registration_disallowed` 错误
- **Cookie 管理**：OAuth 登录前清除注册流程残留的 auth cookies，避免 `invalid_auth_step` 错误
- **Workspace/Org 选择**：完整实现 workspace 选择 → organization 选择 → code 提取链路

## OAuth 登录流程（新增）

注册完成后自动执行的完整 OAuth 登录流程：

```
1/7  GET  /oauth/authorize          — 初始化 OAuth session
2/7  POST /api/accounts/authorize/continue — 提交邮箱
3/7  POST /api/accounts/password/verify    — 提交密码
4/7  POST /api/accounts/email-otp/validate — OTP 验证（如需要）
5/7  GET  continue_url               — 跟随 consent 重定向
6/7  POST /api/accounts/workspace/select   — 选择 Workspace
7/7  POST /oauth/token               — 交换 Authorization Code 获取 Token
```

## 快速开始

### 环境要求

- Python 3.10+

### 安装

```bash
# 克隆项目
git clone https://github.com/917017420/codex-register-fix.git
cd codex-register-fix

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# 安装依赖
pip install -r requirements.txt
```

### 启动

```bash
# 默认启动
python webui.py

# 指定端口
python webui.py --host 0.0.0.0 --port 8899

# 设置访问密码
python webui.py --access-password yourpassword
```

启动后访问 http://127.0.0.1:8000（或自定义端口）

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `APP_HOST` | 监听主机 | `0.0.0.0` |
| `APP_PORT` | 监听端口 | `8000` |
| `APP_ACCESS_PASSWORD` | Web UI 访问密码 | `admin123` |
| `APP_DATABASE_URL` | 数据库连接 | `data/database.db` |

## 功能特性

继承自原项目的全部功能：

- **多邮箱服务**：Tempmail.lol / Outlook / MoeMail / TempMail / DuckMail / FreeMail / IMAP
- **注册模式**：单次注册 / 批量注册 / Outlook 批量注册
- **并发控制**：流水线模式 / 并行模式，最大并发 1-50
- **实时监控**：WebSocket 日志推送
- **代理管理**：动态代理 / 代理列表
- **账号管理**：查看 / 删除 / Token 刷新 / 订阅检测
- **导出格式**：JSON / CSV / CPA / Sub2API
- **支付升级**：Plus / Team 订阅支付链接生成

## 技术栈

| 层级 | 技术 |
|------|------|
| Web 框架 | FastAPI + Uvicorn |
| 数据库 | SQLAlchemy + SQLite / PostgreSQL |
| HTTP 客户端 | curl_cffi（浏览器指纹模拟） |
| 实时通信 | WebSocket |
| 前端 | 原生 JavaScript |

## 致谢

- [cnlimiter/codex-manager](https://github.com/cnlimiter/codex-manager) — 原项目

## License

[MIT](LICENSE)
