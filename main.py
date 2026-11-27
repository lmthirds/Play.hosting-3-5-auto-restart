import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from patchright.sync_api import Page, sync_playwright

from proxy_manager import SingBoxProxy
from telegram_notify import escape_html, send_telegram_report, send_telegram_text

LOGIN_URL = "https://panel.play.hosting/auth/login"
SCREENSHOT_DIR = "screenshots"
BASE_PROXY_PORT = 10808

VIEWPORT_POOL = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1280, "height": 800},
]

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


def mask_email(email: str) -> str:
    if "@" not in email:
        return email[:2] + "***"
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked = local[0] + "***"
    else:
        masked = local[:2] + "***" + local[-1]
    return f"{masked}@{domain}"


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", name or "account")
    return cleaned[:32] or "account"


def parse_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def load_accounts() -> List[Dict[str, str]]:
    raw_json = os.getenv("ACCOUNTS_JSON", "").strip()
    accounts: List[Dict[str, str]] = []

    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"ACCOUNTS_JSON 格式解析失败，请检查 JSON 语法: {e}")

        if not isinstance(parsed, list) or not parsed:
            raise ValueError("ACCOUNTS_JSON 必须是非空 JSON 数组 [...]")

        for i, item in enumerate(parsed, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"ACCOUNTS_JSON 第 {i} 项不是合法对象")
            email = str(item.get("email") or "").strip()
            password = str(item.get("password") or "").strip()
            if not email or not password:
                raise ValueError(f"ACCOUNTS_JSON 第 {i} 项缺少 email 或 password")
            accounts.append(
                {
                    "name": str(item.get("name") or f"账号#{i}").strip(),
                    "email": email,
                    "password": password,
                    "server_id": str(item.get("server_id") or "").strip(),
                    "proxy": str(item.get("proxy") or "").strip(),
                }
            )
        return accounts

    # 向下兼容单账号 Secrets 配置
    single_email = os.getenv("PLAY_EMAIL", "").strip()
    single_pass = os.getenv("PLAY_PASSWORD", "").strip()
    if single_email and single_pass:
        return [
            {
                "name": "默认账号",
                "email": single_email,
                "password": single_pass,
                "server_id": os.getenv("PLAY_SERVER_ID", "").strip(),
                "proxy": os.getenv("PROXY_URL", "").strip(),
            }
        ]

    raise ValueError("未配置 ACCOUNTS_JSON（或单账号 PLAY_EMAIL / PLAY_PASSWORD）")


def is_visible(page: Page, selector: str, timeout: int = 1200) -> bool:
    try:
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


def wait_and_solve_turnstile(page: Page, max_wait: int = 50) -> bool:
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

        elapsed = time.time() - start_ts
        if elapsed > 8 and clicked_times < 3 and int(elapsed) % 10 < 2:
            try:
                iframe_loc = page.locator('iframe[src*="challenges.cloudflare.com"]').first
                box = iframe_loc.bounding_box(timeout=2000)
                if box:
                    target_x = box["x"] + random.uniform(24, 34)
                    target_y = box["y"] + box["height"] / 2 + random.uniform(-3, 3)
                    print(f"[Auth] 尝试人工点击 Turnstile ({int(target_x)}, {int(target_y)})")
                    page.mouse.move(target_x, target_y, steps=random.randint(10, 18))
                    time.sleep(random.uniform(0.25, 0.55))
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
    time.sleep(random.uniform(0.2, 0.5))
    email_input.fill(email)
    time.sleep(random.uniform(0.4, 0.9))

    pass_input.click()
    time.sleep(random.uniform(0.2, 0.5))
    pass_input.fill(password)
    time.sleep(random.uniform(0.5, 1.0))

    wait_and_solve_turnstile(page, max_wait=50)

    login_btn = page.locator('button[type="submit"], button:has-text("Log in")').first
    login_btn.click()
    print("[Auth] 已点击 Log in，等待登录跳转...")

    deadline = time.time() + 35
    while time.time() < deadline:
        if "/auth/login" not in page.url:
            print(f"[Auth] 登录成功，当前页面: {page.url}")
            time.sleep(random.uniform(2.0, 3.5))
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
        print("[Panel] 未指定 server_id，尝试自动打开第一个服务器...")
        first_srv = page.locator('a[href*="/server/"]').first
        first_srv.wait_for(state="visible", timeout=20000)
        first_srv.click()

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
    if is_visible(page, 'text="Server in limbo"', 1000) or is_visible(
        page, 'button:has-text("Wake server")', 800
    ):
        return "limbo"

    if is_visible(page, 'text="Restoring server"', 1000) or is_visible(
        page, 'text="Resuming your server"', 800
    ):
        return "restoring"

    has_stop = is_visible(page, 'button:has-text("Stop")', 1000)
    has_restart = is_visible(page, 'button:has-text("Restart")', 800)
    if has_stop or has_restart:
        body_text = page.locator("body").inner_text()
        if "Starting" in body_text and "Online" not in body_text:
            return "starting"
        return "online"

    if (
        is_visible(page, 'text="Your server is offline"', 1000)
        or is_visible(page, 'button:has-text("Start server")', 800)
        or is_visible(page, 'button:has-text("Start")', 800)
    ):
        return "offline"

    return "unknown"


def click_start_button(page: Page) -> None:
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
            elif starting_since and time.time() - starting_since >= 35:
                return "starting"
        time.sleep(5)

    return detect_server_state(page)


def process_single_account(
    playwright_ctx: Any,
    account: Dict[str, str],
    index: int,
    total: int,
    tg_token: str,
    tg_chat_id: str,
) -> Dict[str, Any]:
    name = account["name"]
    email = account["email"]
    password = account["password"]
    server_id = account["server_id"]
    proxy_url = account["proxy"]

    masked = mask_email(email)
    local_port = BASE_PROXY_PORT + index
    screenshot_path = os.path.join(
        SCREENSHOT_DIR, f"{index:02d}_{sanitize_filename(name)}.png"
    )

    print(f"\n==================== [{index}/{total}] {name} ({masked}) ====================")

    proxy = SingBoxProxy(proxy_url, local_port=local_port)
    actions: List[str] = []
    initial_state = "unknown"
    final_state = "unknown"
    current_url = ""
    error_msg = ""

    try:
        local_proxy = proxy.start()
        viewport = random.choice(VIEWPORT_POOL)

        launch_kwargs: Dict[str, Any] = {
            "headless": False,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                f"--window-size={viewport['width']},{viewport['height']}",
            ],
        }
        if local_proxy:
            launch_kwargs["proxy"] = {"server": local_proxy}

        browser = playwright_ctx.chromium.launch(**launch_kwargs)
        try:
            context = browser.new_context(
                viewport=viewport,
                locale="en-US",
            )
            page = context.new_page()
            page.set_default_timeout(30000)

            try:
                login_play_hosting(page, email, password)
                current_url = open_server_page(page, server_id)

                initial_state = detect_server_state(page)
                print(f"[Status] 初始服务器状态: {initial_state}")
                state = initial_state

                if state == "limbo":
                    wake_btn = page.locator('button:has-text("Wake server")').first
                    wake_btn.click()
                    actions.append("点击 Wake server 唤醒")
                    time.sleep(3)
                    state = wait_for_restore_complete(page)
                elif state == "restoring":
                    actions.append("等待服务器恢复完成")
                    state = wait_for_restore_complete(page)

                if state == "offline":
                    click_start_button(page)
                    actions.append("点击 Start 开机")
                    time.sleep(4)
                    state = wait_for_boot(page)
                elif state == "starting":
                    actions.append("等待启动完成")
                    state = wait_for_boot(page, max_wait=60)
                elif state == "online":
                    actions.append("运行正常，无需操作")

                final_state = state
                time.sleep(2)
                page.screenshot(path=screenshot_path, full_page=False)
                print(f"[Done] {name} 最终状态: {final_state}，截图: {screenshot_path}")

            except Exception as e:
                error_msg = str(e)
                print(f"[Error] {name} 执行异常: {error_msg}")
                try:
                    page.screenshot(path=screenshot_path, full_page=False)
                except Exception:
                    pass
                raise
        finally:
            browser.close()

    except Exception as e:
        if not error_msg:
            error_msg = str(e)
        print(f"[Failed] {name} 任务失败: {error_msg}")
    finally:
        proxy.stop()

    ok = not error_msg and final_state in ("online", "starting")

    if error_msg:
        title = f"❌ [{index}/{total}] {name} 保活失败"
    elif initial_state in ("limbo", "offline", "restoring") and final_state in (
        "starting",
        "online",
    ):
        title = f"🚀 [{index}/{total}] {name} 已自动开机"
    elif ok:
        title = f"✅ [{index}/{total}] {name} 巡检正常"
    else:
        title = f"⚠️ [{index}/{total}] {name} 状态异常"

    proxy_info = (
        f"{proxy.protocol.upper()} ({proxy.exit_ip})"
        if proxy.exit_ip
        else ("直连" if not proxy_url else "代理连接失败")
    )
    action_text = " -> ".join(actions) if actions else "无"

    lines = [
        f"<b>{escape_html(title)}</b>",
        f"👤 账号: <code>{escape_html(masked)}</code>",
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
        lines.append(f"❗ 错误: <code>{escape_html(error_msg[:260])}</code>")

    send_telegram_report(
        bot_token=tg_token,
        chat_id=tg_chat_id,
        caption="\n".join(lines),
        image_path=screenshot_path,
    )

    return {
        "name": name,
        "email": masked,
        "ok": ok,
        "initial_state": initial_state,
        "final_state": final_state,
        "exit_ip": proxy.exit_ip or "N/A",
        "error": error_msg,
    }


def main() -> None:
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    tg_token = os.getenv("TG_BOT_TOKEN", "").strip()
    tg_chat_id = os.getenv("TG_CHAT_ID", "").strip()
    delay_min = max(5.0, parse_float_env("ACCOUNT_DELAY_MIN", 15.0))
    delay_max = max(delay_min, parse_float_env("ACCOUNT_DELAY_MAX", 40.0))
    shuffle_enabled = os.getenv("SHUFFLE_ACCOUNTS", "true").strip().lower() != "false"

    try:
        accounts = load_accounts()
    except ValueError as e:
        print(f"[Fatal] 配置错误: {e}")
        send_telegram_text(
            tg_token,
            tg_chat_id,
            f"❌ <b>Play.Hosting 配置错误</b>\n<code>{escape_html(str(e))}</code>",
        )
        sys.exit(1)

    if shuffle_enabled and len(accounts) > 1:
        random.shuffle(accounts)
        print(f"[Init] 共加载 {len(accounts)} 个账号（已随机打乱巡检顺序）")
    else:
        print(f"[Init] 共加载 {len(accounts)} 个账号")

    # 开场随机小幅抖动，避免每次在整点同一秒发起请求
    start_jitter = random.uniform(3.0, 12.0)
    print(f"[Init] 随机延迟 {start_jitter:.1f}s 后开始巡检...")
    time.sleep(start_jitter)

    results: List[Dict[str, Any]] = []
    total = len(accounts)

    with sync_playwright() as p:
        for idx, account in enumerate(accounts, start=1):
            res = process_single_account(
                playwright_ctx=p,
                account=account,
                index=idx,
                total=total,
                tg_token=tg_token,
                tg_chat_id=tg_chat_id,
            )
            results.append(res)

            # 账号之间的短暂随机空置期（最后一个账号结束后不等待）
            if idx < total:
                cooldown = random.uniform(delay_min, delay_max)
                print(
                    f"[Cooldown] 账号 {account['name']} 处理完毕，空置冷却 {cooldown:.1f}s 后继续下一个账号..."
                )
                time.sleep(cooldown)

    # 多账号时额外发送一条总览消息，方便一眼确认全部结果
    success_count = sum(1 for r in results if r["ok"])
    fail_count = total - success_count

    if total > 1:
        summary_lines = [
            f"<b>📋 Play.Hosting 多账号巡检汇总</b>",
            f"🕒 完成时间: <code>{now_cst()}</code>",
            f"📈 结果: 成功 <b>{success_count}</b> / 失败 <b>{fail_count}</b>（共 {total} 个）",
            "",
        ]
        for r in results:
            icon = "✅" if r["ok"] else "❌"
            line = (
                f"{icon} <b>{escape_html(r['name'])}</b> "
                f"({escape_html(r['initial_state'])} → {escape_html(r['final_state'])}) "
                f"| IP: <code>{escape_html(r['exit_ip'])}</code>"
            )
            summary_lines.append(line)

        send_telegram_text(tg_token, tg_chat_id, "\n".join(summary_lines))

    if fail_count > 0:
        print(f"[Exit] 存在 {fail_count} 个账号执行异常")
        sys.exit(1)

    print("[Exit] 全部账号巡检完成")


if __name__ == "__main__":
    main()
