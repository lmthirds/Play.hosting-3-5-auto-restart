# Play.Hosting Auto Keepalive

通过 GitHub Actions 每天两次自动登录 Play.Hosting 检查服务器状态：若进入 `Server in limbo` 休眠或 `Offline` 停机状态，自动执行 `Wake server` 恢复并点击 `Start` 开机，随后将状态报告与截图推送至 Telegram。支持挂载 `sing-box` 出口代理（VLESS / VMess / Hysteria2 / Trojan / SS）。

## 配置 Secrets

进入仓库 **Settings -> Secrets and variables -> Actions -> New repository secret**，添加以下变量：

| Secret 名称 | 必填 | 说明 |
| :--- | :---: | :--- |
| `PLAY_EMAIL` | 是 | Play.Hosting 登录邮箱 |
| `PLAY_PASSWORD` | 是 | Play.Hosting 登录密码 |
| `PLAY_SERVER_ID` | 推荐 | 面板网址 `/server/` 后面的 ID（例如 `6d69476c`），留空则自动选第一个 |
| `PROXY_URL` | 否 | 代理节点链接（支持 `vless://`、`vmess://`、`hy2://`、`hysteria2://`、`trojan://`、`ss://` 或 sing-box JSON），留空则直连 |
| `TG_BOT_TOKEN` | 否 | Telegram Bot Token（从 `@BotFather` 获取） |
| `TG_CHAT_ID` | 否 | Telegram 接收消息的 Chat ID（可从 `@userinfobot` 获取） |
