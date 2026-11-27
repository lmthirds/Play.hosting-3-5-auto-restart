import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from patchright.sync_api import Page, sync_playwright

from proxy_manager import SingBoxProxy
from telegram_notify import escape_html, send_telegram_report

LOGIN_URL = "https://panel.play.hosting/auth/login"
SCREENSHOT_PATH = "result.png"

STATE_LABELS = {
    "online": "🟢 在线运行中 (Online)",
    "starting": "🟡 正在启动 (Starting)",
    "offline": "🔴 已停机 (Offline)",
    "limbo": "💤 深度休眠 (Server in limbo)",
    "restoring": "⏳ 正在恢复存档 (Restoring)",
    "unknown": "❓ 未知状态 (Unknown)",
}


def now_cst() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S (UTC+8)")


def is_visible(page: Page, selector: str, timeout: int = 1200) -> bool:
    try:
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


def wait_and_solve_turnstile(page: Page, max_wait: int = 45) -> bool:
    print("[Auth] 等待 Cloudflare Turnstile 验证...")
    start_ts = time.time()
    clicked_times = 0

    while time.time() - start_ts < max_wait:
        token = page.evaluate(
            """() => {
                const el = document.querySelector('input[name="cf-turnstile-response"]');
                return el ? el.value : '';
            }"""
        )
        if token and len(token) > 10:
            print("[Auth] Turnstile 验证已通过")
            return True

        # 超过 8 秒仍未自动通过时，尝试点击 Turnstile 复选框
        elapsed = time.time() - start_ts
        if elapsed > 8 and clicked_times < 3 and int(elapsed) % 10 < 2:
            try:
                iframe_loc = page.locator('iframe[src*="challenges.cloudflare.com"]').first
                box = iframe_loc.bounding_box(timeout=2000)
                if box:
                    target_x = box["x"] + 28
                    target_y = box["y"] + box["height"] / 2
                    print(f"[Auth] 尝试人工点击 Turnstile ({int(target_x)}, {int(target_y)})")
                    page.mouse.move(target_x, target_y, steps=12)
                    time.sleep(0.3)
                    page.mouse.click(target_x, target_y)
                    clicked_times += 1
                    time.sleep(4)
                    continue
            except Exception:
                pass

        time.sleep(1.5)

    print("[Auth] Turnstile 等待超时，仍将尝试提交登录")
    return False


def login_play_hosting(page: Page, email: str, password: str) -> None:
    print(f"[Auth] 正在打开登录页: {LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector('input[type="password"]', timeout=30000)

    email_input = page.locator(
        'input[type="email"], input[name="email"], input[autocomplete="email"]'
    ).first
    pass_input = page.locator('input[type="password"]').first

    email_input.click()
    email_input.fill(email)
    time.sleep(0.5)

    pass_input.click()
    pass_input.fill(password)
    time.sleep(0.8)

    wait_and_solve_turnstile(page, max_wait=45)

    login_btn = page.locator('button[type="submit"], button:has-text("Log in")').first
    login_btn.click()
    print("[Auth] 已点击 Log in，等待登录跳转...")

    deadline = time.time() + 35
    while time.time() < deadline:
        if "/auth/login" not in page.url:
            print(f"[Auth] 登录成功，当前页面: {page.url}")
            time.sleep(2)
            return
        time.sleep(1)

    raise RuntimeError("登录超时或失败（可能账号密码错误或未通过 Cloudflare 验证）")


def open_server_page(page: Page, server_id: str) -> str:
    if server_id:
        target_url = (
            server_id
            if server_id.startswith("http")
            else f"https://panel.play.hosting/server/{server_id.strip('/')}"
        )
        if target_url not in page.url:
            print(f"[Panel] 跳转到服务器页面: {target_url}")
            page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
    elif "/server/" not in page.url:
        print("[Panel] 未指定 SERVER_ID，尝试自动打开第一个服务器...")
        first_srv = page.locator('a[href*="/server/"]').first
        first_srv.wait_for(state="visible", timeout=20000)
        first_srv.click()

    # 等待面板核心内容加载完成
    page.wait_for_function(
        """() => {
            const text = document.body ? document.body.innerText : '';
            return text.includes('Server in limbo')
                || text.includes('Restoring server')
                || text.includes('Connect with')
                || text.includes('Offline')
                || text.includes('Online')
                || text.includes('Starting');
        }""",
        timeout=30000,
    )
    time.sleep(3)
    return page.url


def detect_server_state(page: Page) -> str:
    # 1. 优先判断深度休眠（Limbo）和正在恢复（Restoring）
    if is_visible(page, 'text="Server in limbo"', 1000) or is_visible(
        page, 'button:has-text("Wake server")', 800
    ):
        return "limbo"

    if is_visible(page, 'text="Restoring server"', 1000) or is_visible(
        page, 'text="Resuming your server"', 800
    ):
        return "restoring"

    # 2. 判断已启动 / 启动中（右上角出现 Stop 与 Restart 按钮）
    has_stop = is_visible(page, 'button:has-text("Stop")', 1000)
    has_restart = is_visible(page, 'button:has-text("Restart")', 800)
    if has_stop or has_restart:
        body_text = page.locator("body").inner_text()
        if "Starting" in body_text and "Online" not in body_text:
            return "starting"
        return "online"

    # 3. 判断普通关机状态（Offline）
    if (
        is_visible(page, 'text="Your server is offline"', 1000)
        or is_visible(page, 'button:has-text("Start server")', 800)
        or is_visible(page, 'button:has-text("Start")', 800)
    ):
        return "offline"

    return "unknown"


def click_start_button(page: Page) -> None:
    # 优先点击顶部栏的 Start 按钮（不会被底部广告遮挡），其次点击 Start server
    candidates = [
        'header button:has-text("Start")',
        'button:has-text("Start"):not(:has-text("Start server"))',
        'button:has-text("Start server")',
    ]
    for sel in candidates:
        loc = page.locator(sel).first
        try:
            if loc.is_visible(timeout=1500) and loc.is_enabled(timeout=1500):
                loc.scroll_into_view_if_needed()
                loc.click(timeout=5000)
                print(f"[Action] 已点击开机按钮: {sel}")
                return
        except Exception:
            continue

    # 兜底强制点击
    fallback = page.locator('button:has-text("Start")').first
    fallback.click(force=True, timeout=5000)
    print("[Action] 已强制点击 Start 按钮")


def wait_for_restore_complete(page: Page, max_wait: int = 240) -> str:
    print("[Action] 等待服务器从 Limbo 恢复（最长 4 分钟）...")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        state = detect_server_state(page)
        print(f"[Action] 恢复检查当前状态: {state}")
        if state not in ("limbo", "restoring"):
            return state
        time.sleep(6)
    return detect_server_state(page)


def wait_for_boot(page: Page, max_wait: int = 120) -> str:
    print("[Action] 等待服务器启动（最长 2 分钟）...")
    deadline = time.time() + max_wait
    reached_starting = False
    starting_since: Optional[float] = None

    while time.time() < deadline:
        state = detect_server_state(page)
        print(f"[Action] 开机检查当前状态: {state}")
        if state == "online":
            return "online"
        if state == "starting":
            if not reached_starting:
                reached_starting = True
                starting_since = time.time()
            # 进入 Starting 后多等 35 秒，尽量截到 Online 或完整控制台日志
            elif starting_since and time.time() - starting_since >= 35:
                return "starting"
        time.sleep(5)

    return detect_server_state(page)


def main() -> None:
    email = os.getenv("PLAY_EMAIL", "").strip()
    password = os.getenv("PLAY_PASSWORD", "").strip()
    server_id = os.getenv("PLAY_SERVER_ID", "").strip()
    proxy_url = os.getenv("PROXY_URL", "").strip()
    tg_token = os.getenv("TG_BOT_TOKEN", "").strip()
    tg_chat_id = os.getenv("TG_CHAT_ID", "").strip()

    if not email or not password:
        print("[Error] 缺少环境变量 PLAY_EMAIL 或 PLAY_PASSWORD")
        sys.exit(1)

    proxy = SingBoxProxy(proxy_url)
    actions: List[str] = []
    initial_state = "unknown"
    final_state = "unknown"
    current_url = ""
    error_msg = ""

    try:
        local_proxy = proxy.start()

        with sync_playwright() as p:
            launch_kwargs = {
                "headless": False,
                "args": [
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--window-size=1366,768",
                ],
            }
            if local_proxy:
                launch_kwargs["proxy"] = {"server": local_proxy}

            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                viewport={"width": 1366, "height": 768},
                locale="en-US",
            )
            page = context.new_page()

            try:
                login_play_hosting(page, email, password)
                current_url = open_server_page(page, server_id)

                initial_state = detect_server_state(page)
                print(f"[Status] 初始服务器状态: {initial_state}")
                state = initial_state

                # 步骤 A：若处于休眠或恢复中，先唤醒并等待恢复
                if state == "limbo":
                    wake_btn = page.locator('button:has-text("Wake server")').first
                    wake_btn.click()
                    actions.append("点击 Wake server 唤醒休眠服务器")
                    time.sleep(3)
                    state = wait_for_restore_complete(page)
                elif state == "restoring":
                    actions.append("等待服务器恢复完成")
                    state = wait_for_restore_complete(page)

                # 步骤 B：恢复后或原本就是 Offline，则点击开机
                if state == "offline":
                    click_start_button(page)
                    actions.append("点击 Start 启动服务器")
                    time.sleep(4)
                    state = wait_for_boot(page)
                elif state == "starting":
                    actions.append("检测到服务器正在启动，等待就绪")
                    state = wait_for_boot(page, max_wait=60)
                elif state == "online":
                    actions.append("服务器运行正常，无需操作")

                final_state = state
                time.sleep(2)
                page.screenshot(path=SCREENSHOT_PATH, full_page=False)
                print(f"[Done] 最终状态: {final_state}，截图已保存至 {SCREENSHOT_PATH}")

            except Exception as e:
                error_msg = str(e)
                print(f"[Error] 自动化流程异常: {error_msg}")
                try:
                    page.screenshot(path=SCREENSHOT_PATH, full_page=False)
                except Exception:
                    pass
                raise
            finally:
                browser.close()

    except Exception as e:
        if not error_msg:
            error_msg = str(e)
        print(f"[Fatal] 任务执行失败: {error_msg}")
    finally:
        proxy.stop()

    # 组装并推送 Telegram 报告
    if error_msg:
        title = "❌ Play.Hosting 自动保活失败"
    elif initial_state in ("limbo", "offline", "restoring") and final_state in (
        "starting",
        "online",
    ):
        title = "🚀 Play.Hosting 检测到停机，已自动开机"
    elif final_state in ("online", "starting"):
        title = "✅ Play.Hosting 服务器巡检正常"
    else:
        title = "⚠️ Play.Hosting 巡检状态异常"

    proxy_info = (
        f"{proxy.protocol.upper()} ({proxy.exit_ip})"
        if proxy.exit_ip
        else ("直连" if not proxy_url else "代理连接失败")
    )
    action_text = " -> ".join(actions) if actions else "无"

    lines = [
        f"<b>{title}</b>",
        f"🕒 时间: <code>{now_cst()}</code>",
        f"🌐 出口节点: <code>{escape_html(proxy_info)}</code>",
    ]
    if current_url:
        lines.append(f"🔗 面板: <code>{escape_html(current_url)}</code>")
    lines.extend(
        [
            f"📌 初始状态: {STATE_LABELS.get(initial_state, initial_state)}",
            f"🛠 执行动作: {escape_html(action_text)}",
            f"📊 最终状态: {STATE_LABELS.get(final_state, final_state)}",
        ]
    )
    if error_msg:
        lines.append(f"❗ 错误信息: <code>{escape_html(error_msg[:300])}</code>")

    send_telegram_report(
        bot_token=tg_token,
        chat_id=tg_chat_id,
        caption="\n".join(lines),
        image_path=SCREENSHOT_PATH,
    )

    if error_msg or final_state in ("offline", "limbo", "unknown"):
        sys.exit(1)


if __name__ == "__main__":
    main()
