#!/usr/bin/env python3
"""
SeleniumBase UC Mode 版 Grok 注册工具
核心原理：断开 CDP 连接后加载页面/点击，使 Cloudflare 检测不到自动化特征
"""

import os, sys, json, time, logging, argparse, re, random, string
from datetime import datetime
from email_register import get_email_and_token, get_oai_code

# ── logging ──
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

def setup_logging():
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = os.path.join(LOG_DIR, f'sb_run_{ts}.log')
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s | %(levelname)-5s | %(message)s',
        handlers=[logging.StreamHandler(), logging.FileHandler(log_file)]
    )
    return logging.getLogger(__name__)

log = setup_logging()

SSO_DIR = os.getenv('SSO_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sso'))
os.makedirs(SSO_DIR, exist_ok=True)

CHROMIUM_PATH = os.getenv('CHROMIUM_PATH', '/usr/bin/chromium')
PROFILE_DIR = os.getenv('CHROME_PROFILE_DIR', '/data/chrome-profile')
SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"


def ensure_xvfb():
    """确保虚拟显示可用"""
    import glob
    d = os.environ.get('DISPLAY', ':99')
    display_num = d.lstrip(':')
    if glob.glob(f'/tmp/.X11-unix/X{display_num}*'):
        return
    from subprocess import run, DEVNULL
    log.warning(f'Starting Xvfb on {d} ...')
    run(['Xvfb', d, '-screen', '0', '1280x720x24'],
        stdout=DEVNULL, stderr=DEVNULL, timeout=5)
    time.sleep(1)


def start_sb():
    ensure_xvfb()
    from seleniumbase import SB
    sb_ctx = SB(uc=True, incognito=True, headless=False, xvfb=True,
                browser='chrome', binary_location=CHROMIUM_PATH,
                user_data_dir=PROFILE_DIR,
                agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
    sb = sb_ctx.__enter__()
    return sb_ctx, sb


def stop_sb(ctx, sb):
    ctx.__exit__(None, None, None)


def solve_turnstile(sb, label="Turnstile"):
    """尝试用 UC Mode 解决 Turnstile"""
    try:
        log.info(f"[*] 解决 {label}...")
        sb.uc_gui_click_captcha()
        sb.sleep(2)
        log.info(f"[+] {label} 已解决")
        return True
    except Exception as e:
        log.debug(f"{label} uc_gui_click_captcha 失败: {e}")
        try:
            sb.uc_gui_click_cf()
            sb.sleep(2)
            log.info(f"[+] {label} uc_gui_click_cf 已解决")
            return True
        except Exception as e2:
            log.debug(f"{label} uc_gui_click_cf 也失败: {e2}")
            return False


def main():
    parser = argparse.ArgumentParser(description='Grok 注册工具 (SeleniumBase UC)')
    parser.add_argument('--count', type=int, default=1, help='注册轮数')
    args = parser.parse_args()

    log.info("═" * 50)
    log.info("  Grok 注册机启动 (SeleniumBase UC Mode)")
    log.info(f"  轮数: {args.count}")
    log.info("═" * 50)

    for rnd in range(1, args.count + 1):
        log.info(f"--- 第 {rnd}/{args.count} 轮 " + "-" * 30)

        # 创建邮箱
        try:
            email, dev_token = get_email_and_token()
            log.info(f"[*] 邮箱创建成功: {email}")
        except Exception as e:
            log.error(f"创建邮箱失败: {e}")
            continue

        sb_ctx, sb = start_sb()
        try:
            # ── 1. 打开注册页（断开 CDP 连接加载，避免被检测）──
            log.info("[*] 打开 x.ai 注册页 (UC Mode, 断开 CDP 加载)...")
            sb.uc_open_with_reconnect(SIGNUP_URL, reconnect_time=8)
            sb.sleep(3)

            # 调试: 打印可见元素
            debug_html = sb.get_page_source()[:500]
            log.debug(f"页面源码前500字符: {debug_html}")

            # ── 2. 解决初始 Turnstile (如有) ──
            solve_turnstile(sb, "初始 Turnstile")

            # ── 3. 点击 "Sign up with email" ──
            log.info("[*] 点击 Sign up with email...")
            for selector in [
                'button:contains("Sign up with email")',
                'button:contains("sign up with email")',
                'button:contains("Email")',
            ]:
                try:
                    sb.uc_click(selector, reconnect_time=3)
                    log.info(f"[*] 已点击: {selector}")
                    sb.sleep(2)
                    break
                except Exception:
                    continue

            # ── 4. 填写邮箱 ──
            log.info(f"[*] 填写邮箱: {email}")
            email_filled = False
            for selector in ['input[name="email"]', 'input[type="email"]']:
                try:
                    if sb.is_element_visible(selector):
                        sb.type(selector, email)
                        email_filled = True
                        log.info(f"[*] 已填入邮箱: {email}")
                        break
                except Exception:
                    continue
            if not email_filled:
                log.warning("未找到邮箱输入框，尝试直接 JS 注入")
                try:
                    sb.execute_script(f"document.querySelector('input[name=\"email\"]').value='{email}';")
                    email_filled = True
                except Exception as e:
                    log.error(f"JS 注入邮箱失败: {e}")
                    continue

            sb.sleep(1)

            # ── 5. 点击提交（断开 CDP，Turnstile 检测不到自动化）──
            log.info("[*] 点击提交按钮 (UC Mode)...")
            try:
                sb.uc_click('button[type="submit"]', reconnect_time=5)
                log.info("[*] 已点击提交")
            except Exception:
                try:
                    sb.uc_click('button:contains("Sign up")', reconnect_time=5)
                except Exception as e:
                    log.warning(f"点击提交按钮失败: {e}")

            sb.sleep(3)

            # ── 6. 再次处理 Turnstile (提交后可能出现) ──
            solve_turnstile(sb, "提交后 Turnstile")

            # ── 7. 等待验证码 ──
            log.info("[*] 等待验证码...")
            code = get_oai_code(dev_token, email, timeout=120)
            if not code:
                log.error("验证码超时")
                continue
            log.info(f"[*] 验证码: {code}")

            sb.sleep(2)

            # ── 8. 填入验证码 ──
            code_filled = False
            for selector in [
                'input[name="code"]',
                'input[autocomplete="one-time-code"]',
                'input[inputmode="numeric"]',
            ]:
                try:
                    if sb.is_element_visible(selector):
                        sb.type(selector, code)
                        code_filled = True
                        log.info(f"[*] 已填入验证码")
                        break
                except Exception:
                    continue
            if not code_filled:
                log.warning("未找到验证码输入框")
                # 尝试遍历所有输入框
                inputs = sb.find_elements('input:not([type="hidden"])')
                if inputs and len(inputs) > 0:
                    try:
                        sb.type(inputs[0], code)
                        code_filled = True
                    except Exception:
                        pass

            sb.sleep(1)

            # ── 9. 点击验证按钮 ──
            try:
                sb.uc_click('button[type="submit"]', reconnect_time=3)
                log.info("[*] 已提交验证码")
            except Exception:
                try:
                    sb.uc_click('button:contains("Verify")', reconnect_time=3)
                except Exception:
                    pass

            sb.sleep(3)

            # ── 10. 检查是否跳转到个人资料页 ──
            current_url = sb.get_current_url()
            log.info(f"[*] 验证后 URL: {current_url}")

            if 'sign-up' in current_url or 'register' in current_url:
                log.info("[*] 填写个人资料...")

                # 姓名
                fn = random.choice(['Alex','Sam','Jordan','Taylor','Morgan','Casey','Riley','Quinn','Avery','Jamie'])
                ln = random.choice(['Smith','Johnson','Williams','Brown','Jones','Garcia','Miller','Davis','Wilson','Moore'])
                for selector in [
                    'input[name="givenName"]',
                    'input[autocomplete="given-name"]',
                    'input[name="firstName"]',
                    'input[placeholder*="first" i]',
                    'input[placeholder*="name" i]',
                ]:
                    try:
                        if sb.is_element_visible(selector):
                            sb.type(selector, fn)
                            log.info(f"[*] 已填入名: {fn}")
                            break
                    except Exception:
                        continue

                for selector in [
                    'input[name="familyName"]',
                    'input[autocomplete="family-name"]',
                    'input[name="lastName"]',
                    'input[placeholder*="last" i]',
                ]:
                    try:
                        if sb.is_element_visible(selector):
                            sb.type(selector, ln)
                            log.info(f"[*] 已填入姓: {ln}")
                            break
                    except Exception:
                        continue

                # 用户名
                uname = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
                for selector in [
                    'input[name="username"]',
                    'input[autocomplete="username"]',
                    'input[placeholder*="username" i]',
                ]:
                    try:
                        if sb.is_element_visible(selector):
                            sb.type(selector, uname)
                            log.info(f"[*] 已填入用户名: {uname}")
                            break
                    except Exception:
                        continue

                # 密码
                pwd = ''.join(random.choices(string.ascii_letters + string.digits + '!@#$', k=16))
                for selector in [
                    'input[name="password"]',
                    'input[type="password"]',
                    'input[autocomplete="new-password"]',
                ]:
                    try:
                        if sb.is_element_visible(selector):
                            sb.type(selector, pwd)
                            log.info(f"[*] 已填入密码")
                            break
                    except Exception:
                        continue

                # 个人资料页 Turnstile
                solve_turnstile(sb, "个人资料页 Turnstile")

                # 提交
                try:
                    sb.uc_click('button[type="submit"]', reconnect_time=5)
                    log.info("[*] 已提交个人资料")
                except Exception:
                    try:
                        sb.uc_click('button:contains("Complete")', reconnect_time=5)
                    except Exception:
                        pass

                sb.sleep(5)

            # ── 11. 提取 SSO ──
            final_url = sb.get_current_url()
            log.info(f"[*] 最终 URL: {final_url}")

            cookies = sb.driver.get_cookies()
            log.info(f"[*] Cookies: {len(cookies)} 个")
            for c in cookies:
                name_lower = c['name'].lower()
                if 'sso' in name_lower or 'token' in name_lower or 'session' in name_lower or 'auth' in name_lower:
                    log.info(f"[+] 关键 Cookie: {c['name']} = {c['value'][:40]}...")

        except Exception as e:
            log.error(f"注册过程异常: {e}", exc_info=True)
        finally:
            stop_sb(sb_ctx, sb)

        log.info(f"[*] 第 {rnd} 轮完成")

    log.info("═" * 50)
    log.info("  所有轮次完成")
    log.info("═" * 50)


if __name__ == '__main__':
    main()
