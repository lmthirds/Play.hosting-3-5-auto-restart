# Play.Hosting 多账号自动保活助手

基于 **GitHub Actions + Patchright + sing-box** 的 Play.Hosting 自动化巡检与开机工具。支持每天定时登录多个账号，自动识别 `Server in limbo`（深度休眠）与 `Offline`（关机）状态并依次执行唤醒、恢复等待与开机操作，完成后通过 Telegram 推送每个账号的执行结果、实时截图与总览报告。

---

## 核心特性

* **多账号完全隔离**：采用「一账号 + 一专属代理 + 一独立浏览器实例」架构，每个账号分配独立的 `sing-box` 临时配置与本地端口，结束后彻底销毁进程与上下文，杜绝 Cookie、缓存及出口 IP 串号。
* **多协议 sing-box 代理**：内置节点解析器，直接填入分享链接即可启动代理，支持 `VLESS`（含 Reality / WS / gRPC / HTTPUpgrade）、`VMess`、`Hysteria2`（`hy2://` / `hysteria2://`，含 Salamander 混淆）、`Trojan`、`Shadowsocks` 及原生 `sing-box` JSON。
* **防检测风控策略**：
  * 任务开场前随机延迟 `3~12` 秒，错开整点 Cron 并发特征。
  * 每次巡检默认随机打乱账号执行顺序。
  * 账号切换之间自动进入随机空置冷却期（默认 `15~40` 秒）。
  * 模拟随机桌面分辨率与带偏移的人工点击轨迹，自动应对 Cloudflare Turnstile 验证。
* **全状态流转支持**：自动覆盖 `登录验证` → `Server in limbo (Wake server)` → `Restoring server` → `Offline (Start / Start server)` → `Starting / Online` 全链路，并自动规避底部广告遮挡。
* **Telegram 实时推送**：强制直连 Telegram API（不走代理，防止节点波动导致漏报），单账号发送脱敏图文报告，多账号额外发送汇总清单；同时在 Actions Artifacts 留存截图供排错。
* **故障隔离**：单个账号代理失效或登录异常不会中断后续账号的巡检流程。

---

## 项目结构

```text
play-hosting-keeper/
├── .github/
│   └── workflows/
│       └── keepalive.yml    # GitHub Actions 定时任务与环境安装脚本
├── main.py                  # 多账号调度、面板状态机与防检测核心逻辑
├── proxy_manager.py         # sing-box 多协议链接解析与独立进程管理
├── telegram_notify.py       # Telegram 图文与汇总推送模块
├── requirements.txt         # Python 依赖清单
└── README.md                # 项目说明文档
```

---

## 快速部署指南

### 1. 创建私有仓库

> **强烈建议设为 Private（私有仓库）**：虽然敏感信息存放在 Secrets 中，但仓库设为私有可同时保护运行日志和截图 Artifacts 不被他人查看。

将本项目全部文件上传至你的 GitHub 私有仓库。

### 2. 配置 GitHub Secrets

打开仓库页面，进入 **Settings** → **Secrets and variables** → **Actions** → **New repository secret**，添加以下 3 个 Secret：

| Secret 名称 | 是否必填 | 说明 |
| :--- | :---: | :--- |
| `ACCOUNTS_JSON` | **必填** | 多账号与专属代理节点的 JSON 数组配置（详见下方模板） |
| `TG_BOT_TOKEN` | 选填 | Telegram 机器人 Token（通过 `@BotFather` 创建获取） |
| `TG_CHAT_ID` | 选填 | 接收通知的 Chat ID（通过 `@userinfobot` 获取，群组为负数） |

### 3. 编写 `ACCOUNTS_JSON`

将下方模板修改为你的实际账号与代理节点后，完整粘贴到 `ACCOUNTS_JSON` 中：

```json
[
  {
    "name": "UK节点-主号",
    "email": "account1@outlook.com",
    "password": "your_password_1",
    "server_id": "6d69476c",
    "proxy": "vless://uuid@example1.com:443?encryption=none&security=reality&sni=[www.microsoft.com](https://www.microsoft.com)&fp=chrome&pbk=YOUR_PBK&sid=YOUR_SID&type=tcp&flow=xtls-rprx-vision#node1"
  },
  {
    "name": "US节点-小号",
    "email": "account2@outlook.com",
    "password": "your_password_2",
    "server_id": "8a1234bc",
    "proxy": "hy2://your_password@example2.com:443?sni=example2.com&insecure=0#node2"
  },
  {
    "name": "EU节点-三号",
    "email": "account3@outlook.com",
    "password": "your_password_3",
    "server_id": "",
    "proxy": "vmess://eyJ2IjoiMiIsInBzIjoi..."
  }
]
```

#### 字段说明

| 字段名 | 必填 | 说明 |
| :--- | :---: | :--- |
| `name` | 否 | 自定义账号备注（用于日志、截图命名和 Telegram 通知展示） |
| `email` | **是** | Play.Hosting 登录邮箱 |
| `password` | **是** | Play.Hosting 登录密码 |
| `server_id` | 推荐 | 浏览器打开服务器控制台后，网址 `/server/` 后面的字符串（如 `panel.play.hosting/server/6d69476c` 中的 `6d69476c`）；留空则自动选择列表中的第一个服务器 |
| `proxy` | 否 | 该账号绑定的专属出口节点链接或 `sing-box` JSON；留空则使用 GitHub Runner 直连 |

### 4. 手动测试运行

1. 点击仓库顶部 **Actions** 选项卡，若提示启用工作流则点击启用。
2. 在左侧选择 **Play.Hosting Multi-Account Keepalive**。
3. 点击右侧 **Run workflow** 手动触发一次，观察日志并确认 Telegram 收到各账号的独立出口 IP 与面板截图。

---

## 支持的代理格式

`proxy` 字段支持直接粘贴主流客户端导出的分享链接：

* **VLESS**：`vless://...`（支持 TCP / Reality / WebSocket / gRPC / HTTPUpgrade / Vision 流控）
* **VMess**：`vmess://...`（标准 Base64 JSON 格式，自动兼容 TLS / WS 早数据 `?ed=`）
* **Hysteria2**：`hy2://...` 或 `hysteria2://...`（支持端口跳跃首位提取与 Salamander 混淆）
* **Trojan**：`trojan://...`
* **Shadowsocks**：`ss://...`
* **自定义 JSON**：直接粘贴单节点 `outbound` 对象或完整 `sing-box` 配置 JSON（需注意 JSON 字符串转义）

---

## 高级配置

如需调整执行频率或防检测空置时间，直接编辑 `.github/workflows/keepalive.yml`：

### 1. 修改定时巡检时间

工作流默认在北京时间 **08:00** 和 **20:00**（UTC `00:00` 和 `12:00`）各执行一次：

```yaml
on:
  schedule:
    # 格式：分 时 日 月 周（使用 UTC 时间，北京时间需减 8 小时）
    - cron: "0 0,12 * * *"
```

> **提示**：GitHub Actions 定时任务在高峰期可能存在数分钟调度延迟；如果账号数量超过 5 个，建议适当调大 `timeout-minutes`（默认 `30` 分钟）。

### 2. 调整防检测环境变量

```yaml
env:
  ACCOUNT_DELAY_MIN: "15"   # 账号切换最短空置冷却时间（秒）
  ACCOUNT_DELAY_MAX: "40"   # 账号切换最长空置冷却时间（秒）
  SHUFFLE_ACCOUNTS: "true"  # 是否随机打乱账号执行顺序（true / false）
```

---

## 常见问题排查

**1. 提示 `ACCOUNTS_JSON 格式解析失败`**
* 检查 JSON 数组最后一个对象后面是否多写了逗号，或密码/代理链接中是否包含未转义的双引号 `"`。建议使用在线 JSON 校验工具检查后再粘贴。

**2. `sing-box` 启动失败或代理连通性检测失败**
* 确认节点本身未过期且在海外网络（GitHub Runner 位于美国数据中心）可正常连接。
* 若节点使用自签名证书，请确保链接参数中包含 `allowInsecure=1` 或 `insecure=1`。

**3. 登录页卡在 Cloudflare Turnstile 验证**
* 部分数据中心出口 IP 容易触发严格人机校验，建议为该账号更换纯净度更高的住宅/冷门 VPS 代理节点。

**4. 如何查看执行失败时的页面状态？**
* 每次运行结束后（无论成功或失败），工作流都会把所有账号的最终截图打包上传到该次 Run 底部的 **Artifacts → `play-hosting-screenshots`**（保留 3 天），下载即可定位卡在哪一步。

---

## 免责声明与安全建议

* 本项目仅用于个人学习与服务器日常巡检维护，请合理设置巡检频率，遵守服务商的使用条款。
* 请勿将真实账号密码、代理链接或 Telegram Token 直接写入仓库代码文件，务必全部通过 **GitHub Secrets** 注入。
