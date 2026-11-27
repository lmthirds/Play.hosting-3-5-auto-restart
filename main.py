import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from seleniumbase import SB

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
            raise ValueError("ACCOUNTS_JSON 必须是非空 JSON 数组")

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


def get_body_text(sb: Any) -> str:
    try:
        return sb.get_text("body") or ""
    except Exception:
        return ""


def get_turnstile_token(sb: Any) -> str:
    try:
        token = sb.execute_script(
            """
            const el = document.querySelector('input[name="cf-turnstile-response"]');
            return el ? el.value : '';
            """
        )
        return str(token or "")
    except Exception:
        return ""


def try_uc_gui_click(sb: Any) -> None:
    try:
        print("[Auth] 执行系统级 GUI 点击 Cloudflare 验证框...")
        sb.uc_gui_click_captcha()
    except Exception as e:
        print(f"[Auth] GUI 点击尝试返回异常（可忽略）: {e}")


def wait_for_login_form_or_bypass_cf(sb: Any, max_wait: int = 75) -> None:
    print("[Auth] 等待登录表单加载（UC 模式自动过 Cloudflare 全屏盾）...")
    start_ts = time.time()
    last_title = ""
    clicked_times = 0
    last_click_ts = 0.0

    while time.time() - start_ts < max_wait:
        if sb.is_element_visible('input[type="password"]'):
            print("[Auth] 登录表单已加载完成")
            return

        current_url = ""
        try:
            current_url = sb.get_current_url() or ""
        except Exception:
            pass

        if "/server/" in current_url:
            print("[Auth] 页面已直接跳转至服务器面板")
            return

        try:
            title = sb.get_title() or ""
        except Exception:
            title = ""

        if title != last_title:
            print(f"[Auth] 当前页面标题: {title!r} | URL: {current_url}")
            last_title = title

        elapsed = time.time() - start_ts
        lower_title = title.lower()
        is_cf_shield = any(
            kw in lower_title
            for kw in ("just a moment", "attention required", "cloudflare", "请稍候", "verify")
        )

        # 全屏盾出现时，调用系统级鼠标点击，每次间隔至少 12 秒
        if (
            is_cf_shield
            and clicked_times < 4
            and elapsed > 4
            and (time.time() - last_click_ts >= 12)
        ):
            try_uc_gui_click(sb)
            clicked_times += 1
            last_click_ts = time.time()
            sb.sleep(5)
            continue

        sb.sleep(2)

    snippet = re.sub(r"\s+", " ", get_body_text(sb).strip())[:200]
    raise RuntimeError(
        f"等待登录表单超时({max_wait}s) | Title: {last_title!r} | 页面文本: {snippet}"
    )


def wait_and_solve_login_turnstile(sb: Any, max_wait: int = 50) -> bool:
    print("[Auth] 等待登录页 Cloudflare Turnstile 验证...")
    start_ts = time.time()
    clicked_times = 0
    last_click_ts = 0.0

    while time.time() - start_ts < max_wait:
        token = get_turnstile_token(sb)
        if len(token) > 10:
            print("[Auth] 登录页 Turnstile 验证已通过")
            return True

        elapsed = time.time() - start_ts
        if elapsed > 6 and clicked_times < 3 and (time.time() - last_click_ts >= 12):
            try_uc_gui_click(sb)
            clicked_times += 1
            last_click_ts = time.time()
            sb.sleep(5)
            continue

        sb.sleep(2)

    print("[Auth] 登录页 Turnstile 等待超时，仍将尝试提交登录")
    return False


def login_play_hosting(sb: Any, email: str, password: str) -> None:
    print(f"[Auth] 正在以 UC 断连模式打开登录页: {LOGIN_URL}")
    sb.uc_open_with_reconnect(LOGIN_URL, 6)

    wait_for_login_form_or_bypass_cf(sb, max_wait=75)
    if "/auth/login" not in (sb.get_current_url() or ""):
        return

    email_sel = 'input[type="email"], input[name="email"], input[autocomplete="email"]'
    pass_sel = 'input[type="password"]'

    sb.click(email_sel)
    sb.sleep(random.uniform(0.2, 0.5))
    sb.type(email_sel, email)
    sb.sleep(random.uniform(0.4, 0.9))

    sb.click(pass_sel)
    sb.sleep(random.uniform(0.2, 0.5))
    sb.type(pass_sel, password)
    sb.sleep(random.uniform(0.5, 1.0))

    wait_and_solve_login_turnstile(sb, max_wait=50)

    login_btn_xpath = '//button[@type="submit" or contains(normalize-space(.), "Log in")]'
    sb.click(login_btn_xpath)
    print("[Auth] 已点击 Log in，等待登录跳转...")

    deadline = time.time() + 35
    while time.time() < deadline:
        cur_url = sb.get_current_url() or ""
        if "/auth/login" not in cur_url:
            print(f"[Auth] 登录成功，当前页面: {cur_url}")
            sb.sleep(random.uniform(2.0, 3.5))
            return
        sb.sleep(1.5)

    raise RuntimeError("登录超时或失败（可能账号密码错误或未通过 Cloudflare 验证）")


def open_server_page(sb: Any, server_id: str) -> str:
    cur_url = sb.get_current_url() or ""
    if server_id:
        target_url = (
            server_id
            if server_id.startswith("http")
            else f"https://panel.play.hosting/server/{server_id.strip('/')}"
        )
        if target_url not in cur_url:
            print(f"[Panel] 跳转到服务器页面: {target_url}")
            sb.uc_open_with_reconnect(target_url, 4)
    elif "/server/" not in cur_url:
        print("[Panel] 未指定 server_id，尝试自动打开第一个服务器...")
        sb.wait_for_element_visible('a[href*="/server/"]', timeout=20)
        sb.click('a[href*="/server/"]')

    # 等待面板核心状态文案渲染
    deadline = time.time() + 40
    keywords = (
        "Server in limbo",
        "Restoring server",
        "Connect with",
        "Offline",
        "Online",
        "Starting",
    )
    while time.time() < deadline:
        text = get_body_text(sb)
        if any(k in text for k in keywords):
            break
        sb.sleep(1.5)

    sb.sleep(3)
    return sb.get_current_url() or ""


def detect_server_state(sb: Any) -> str:
    if sb.is_element_visible('//*[contains(text(), "Server in limbo")]') or sb.is_element_visible(
        '//button[contains(., "Wake server")]'
    ):
        return "limbo"

    if sb.is_element_visible('//*[contains(text(), "Restoring server")]') or sb.is_element_visible(
        '//*[contains(text(), "Resuming your server")]'
    ):
        return "restoring"

    has_stop = sb.is_element_visible('//button[contains(., "Stop")]')
    has_restart = sb.is_element_visible('//button[contains(., "Restart")]')
    if has_stop or has_restart:
        body_text = get_body_text(sb)
        if "Starting" in body_text and "Online" not in body_text:
            return "starting"
        return "online"

    if (
        sb.is_element_visible('//*[contains(text(), "Your server is offline")]')
        or sb.is_element_visible('//button[contains(., "Start server")]')
        or sb.is_element_visible('//button[contains(., "Start")]')
    ):
        return "offline"

    return "unknown"


def click_start_button(sb: Any) -> None:
    candidates = [
        '//header//button[contains(., "Start")]',
        '//button[contains(., "Start") and not(contains(., "Start server"))]',
        '//button[contains(., "Start server")]',
        '//button[contains(., "Start")]',
    ]
    for xpath in candidates:
        try:
            if sb.is_element_visible(xpath):
                sb.scroll_to(xpath)
                sb.click(xpath)
                print(f"[Action] 已点击开机按钮: {xpath}")
                return
        except Exception:
            continue

    # 兜底 JS 点击
    sb.execute_script(
        """
        const btns = Array.from(document.querySelectorAll('button'));
        const target = btns.find(b => (b.innerText || '').includes('Start') && !b.disabled);
        if (target) target.click();
        """
    )
    print("[Action] 已通过 JS 兜底点击 Start 按钮")


def wait_for_restore_complete(sb: Any, max_wait: int = 240) -> str:
    print("[Action] 等待服务器从 Limbo 恢复（最长 4 分钟）...")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        state = detect_server_state(sb)
        print(f"[Action] 恢复检查当前状态: {state}")
        if state not in ("limbo", "restoring"):
            return state
        sb.sleep(6)
    return detect_server_state(sb)


def wait_for_boot(sb: Any, max_wait: int = 120) -> str:
    print("[Action] 等待服务器启动（最长 2 分钟）...")
    deadline = time.time() + max_wait
    reached_starting = False
    starting_since: Optional[float] = None

    while time.time() < deadline:
        state = detect_server_state(sb)
        print(f"[Action] 开机检查当前状态: {state}")
        if state == "online":
            return "online"
        if state == "starting":
            if not reached_starting:
                reached_starting = True
                starting_since = time.time()
            elif starting_since and time.time() - starting_since >= 35:
                return "starting"
        sb.sleep(5)

    return detect_server_state(sb)


def process_single_account(
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
    user_data_dir = tempfile.mkdtemp(prefix=f"ph_uc_profile_{index}_")
    actions: List[str] = []
    initial_state = "unknown"
    final_state = "unknown"
    current_url = ""
    error_msg = ""

    try:
        local_proxy = proxy.start()
        sb_proxy = (
            local_proxy.replace("http://", "").replace("https://", "")
            if local_proxy
            else None
        )
        viewport = random.choice(VIEWPORT_POOL)

        with SB(
            uc=True,
            headless=False,
            proxy=sb_proxy,
            user_data_dir=user_data_dir,
            locale_code="en",
        ) as sb:
            sb.set_window_size(viewport["width"], viewport["height"])

            try:
                login_play_hosting(sb, email, password)
                current_url = open_server_page(sb, server_id)

                initial_state = detect_server_state(sb)
                print(f"[Status] 初始服务器状态: {initial_state}")
                state = initial_state

                if state == "limbo":
                    sb.click('//button[contains(., "Wake server")]')
                    actions.append("点击 Wake server 唤醒")
                    sb.sleep(3)
                    state = wait_for_restore_complete(sb)
                elif state == "restoring":
                    actions.append("等待服务器恢复完成")
                    state = wait_for_restore_complete(sb)

                if state == "offline":
                    click_start_button(sb)
                    actions.append("点击 Start 开机")
                    sb.sleep(4)
                    state = wait_for_boot(sb)
                elif state == "starting":
                    actions.append("等待启动完成")
                    state = wait_for_boot(sb, max_wait=60)
                elif state == "online":
                    actions.append("运行正常，无需操作")

                final_state = state
                sb.sleep(2)
                sb.save_screenshot(screenshot_path)
                print(f"[Done] {name} 最终状态: {final_state}，截图: {screenshot_path}")

            except Exception as e:
                error_msg = str(e)
                print(f"[Error] {name} 执行异常: {error_msg}")
                try:
                    sb.save_screenshot(screenshot_path)
                except Exception:
                    pass
                raise

    except Exception as e:
        if not error_msg:
            error_msg = str(e)
        print(f"[Failed] {name} 任务失败: {error_msg}")
    finally:
        proxy.stop()
        shutil.rmtree(user_data_dir, ignore_errors=True)

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
        lines.append(f"❗ 错误: <code>{escape_html(error_msg[:300])}</code>")

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

    start_jitter = random.uniform(3.0, 12.0)
    print(f"[Init] 随机延迟 {start_jitter:.1f}s 后开始巡检...")
    time.sleep(start_jitter)

    results: List[Dict[str, Any]] = []
    total = len(accounts)

    for idx, account in enumerate(accounts, start=1):
        res = process_single_account(
            account=account,
            index=idx,
            total=total,
            tg_token=tg_token,
            tg_chat_id=tg_chat_id,
        )
        results.append(res)

        if idx < total:
            cooldown = random.uniform(delay_min, delay_max)
            print(
                f"[Cooldown] 账号 {account['name']} 处理完毕，空置冷却 {cooldown:.1f}s 后继续下一个账号..."
            )
            time.sleep(cooldown)

    success_count = sum(1 for r in results if r["ok"])
    fail_count = total - success_count

    if total > 1:
        summary_lines = [
            "<b>📋 Play.Hosting 多账号巡检汇总</b>",
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
