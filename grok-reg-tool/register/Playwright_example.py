#!/usr/bin/env python3
"""
Playwright + Stealth 版 Grok 注册工具
"""

import os, sys, json, time, logging, argparse, re, random, string
from datetime import datetime

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

from email_register import get_email_and_token, get_oai_code

HAVE_STEALTH = False
try:
    from playwright_stealth import stealth_sync
    HAVE_STEALTH = True
except ImportError:
    pass

# ── logging ──
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

def setup_logging():
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = os.path.join(LOG_DIR, f'pw_run_{ts}.log')
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s | %(levelname)-5s | %(message)s',
        handlers=[logging.StreamHandler(), logging.FileHandler(log_file)]
    )
    return logging.getLogger(__name__)

log = setup_logging()

# ── config ──
SSO_DIR = os.getenv('SSO_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sso'))
os.makedirs(SSO_DIR, exist_ok=True)

CHROMIUM_PATH = os.getenv('CHROMIUM_PATH', '/usr/bin/chromium')
TURNSTILE_SITEKEY = os.getenv('TURNSTILE_SITEKEY', '0x4AAAAAAAhr9JGVDZbrZOo0')

# ── stealth init script (injected before any page JS) ──
STEALTH_INIT_JS = r"""
(function() {
    if (window.__pwStealthPatched) return;
    window.__pwStealthPatched = true;

    const OD = Object.defineProperty;

    // 1. navigator.webdriver
    try { OD(navigator, 'webdriver', { get: () => false }); } catch(e) {}

    // 2. navigator.plugins
    try {
        const fakePlugins = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }
        ];
        fakePlugins.item = i => fakePlugins[i] || null;
        fakePlugins.namedItem = n => null;
        fakePlugins.refresh = () => {};
        OD(navigator, 'plugins', { get: () => fakePlugins });
    } catch(e) {}

    // 3. navigator.languages
    try { OD(navigator, 'languages', { get: () => ['en-US', 'en'] }); } catch(e) {}

    // 4. hardwareConcurrency
    try { OD(navigator, 'hardwareConcurrency', { get: () => 8 }); } catch(e) {}

    // 5. deviceMemory
    try { OD(navigator, 'deviceMemory', { get: () => 8 }); } catch(e) {}

    // 6. WebGL vendor/renderer
    try {
        const origGetCtx = HTMLCanvasElement.prototype.getContext;
        HTMLCanvasElement.prototype.getContext = function(...args) {
            const ctx = origGetCtx.apply(this, args);
            if (ctx && (args[0] === 'webgl' || args[0] === 'webgl2')) {
                const origGetParam = ctx.getParameter.bind(ctx);
                ctx.getParameter = function(p) {
                    if (p === 37445) return 'Intel Inc.';
                    if (p === 37446) return 'Intel Iris OpenGL Engine';
                    if (p === 7936) return 'WebGL GLSL ES 3.00 (OpenGL ES GLSL ES 3.0 Chromium)';
                    if (p === 7937) return 'OpenGL ES 3.0 (WebGL 2.0 Chromium)';
                    return origGetParam(p);
                };
            }
            return ctx;
        };
    } catch(e) {}

    // 7. chrome.runtime
    try {
        if (window.chrome && !window.chrome.runtime) {
            window.chrome.runtime = {};
        }
    } catch(e) {}

    // 8. permissions
    try {
        if (navigator.permissions) {
            const origQuery = navigator.permissions.query.bind(navigator.permissions);
            navigator.permissions.query = async (desc) => {
                const result = await origQuery(desc);
                if (desc.name === 'notifications') result.state = 'prompt';
                return result;
            };
        }
    } catch(e) {}
})();
"""

# ── helper: email (uses email_register.py) ──


# ── element helpers ──

def find_visible(page, selectors, timeout=3000):
    """Return first visible locator matching any selector."""
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.is_visible(timeout=timeout):
                return loc
        except Exception:
            continue
    return None


# ── Turnstile ──

def debug_turnstile_state(page, phase):
    url = page.url
    has_ts = page.evaluate("typeof turnstile !== 'undefined'")
    widgets = 0
    try:
        widgets = page.evaluate("window.turnstile && typeof turnstile.getWidgets === 'function' ? turnstile.getWidgets().length : 0")
    except Exception:
        pass
    cf_resp = page.evaluate("(document.querySelector('[name=cf-turnstile-response]') || {}).value || ''")
    has_iframe = page.evaluate("document.querySelector('iframe[src*=\"challenges.cloudflare.com\"]') !== null")

    log.debug(f"[Debug] 页面状态 ({phase}): URL={url}")
    log.debug(f"  turnstile={'是' if has_ts else '否'}, widgets={widgets}, resp={cf_resp[:30]}")
    log.debug(f"  iframe={'存在' if has_iframe else '不存在'}")


def inject_turnstile_api(page):
    has = page.evaluate("typeof turnstile !== 'undefined'")
    if has:
        return True
    try:
        page.evaluate("new Promise(function(ok, fail){var s=document.createElement('script');s.src='https://challenges.cloudflare.com/turnstile/v0/api.js';s.onload=ok;s.onerror=fail;document.head.appendChild(s);})")
        page.wait_for_timeout(3000)
        return page.evaluate("typeof turnstile !== 'undefined'")
    except Exception as e:
        log.warning(f"注入 Turnstile API 失败: {e}")
        return False


def inject_turnstile_widget(page, container_id='cf-turnstile-inject'):
    cid_lit = json.dumps(container_id)
    sk_lit = json.dumps(TURNSTILE_SITEKEY)
    has = page.evaluate("!!document.getElementById(" + cid_lit + ")")
    if not has:
        page.evaluate("(function(){var d=document.createElement('div');d.id=" + cid_lit + ";document.body.appendChild(d);})()")
    wid = page.evaluate("(function(){window.__cf_token='';window.__cf_error='';var w=turnstile.render('#" + container_id + "',{sitekey:" + sk_lit + ",callback:function(t){window.__cf_token=t;},'error-callback':function(e){window.__cf_error=e||'unknown';},'timeout-callback':function(){window.__cf_error='timeout';}});return w;})()")
    return wid


def wait_for_token(page, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = page.evaluate("window.__cf_token || ''")
        if t:
            return t
        t = page.evaluate("(document.querySelector('[name=cf-turnstile-response]')||{}).value||''")
        if t:
            return t
        page.wait_for_timeout(500)
    return None


def click_turnstile_iframe(page, container_id='cf-turnstile-inject'):
    # 1) Playwright frame_locator (works cross-origin via CDP)
    try:
        selector = f'#{container_id} iframe'
        page.wait_for_selector(selector, timeout=5000)
        frame = page.frame_locator(selector)
        if frame:
            for cb_sel in ['#checkbox', '.challenge-button', 'div[role="checkbox"]', 'label', 'button']:
                try:
                    cb = frame.locator(cb_sel).first
                    if cb.is_visible(timeout=2000):
                        cb.click(timeout=3000)
                        log.info(f"[+] Playwright frame_locator 点击 '{cb_sel}' 成功")
                        return True
                except Exception:
                    continue
    except Exception as e:
        log.debug(f"Playwright frame_locator 点击失败: {e}")

    # 2) dispatchEvent fallback
    try:
        cid_esc = container_id.replace("'", "\\'")
        page.evaluate("(function(){var ifr=document.querySelector('#" + cid_esc + " iframe');if(!ifr)return;var r=ifr.getBoundingClientRect();var x=r.left+r.width/2,y=r.top+r.height/2;['mousedown','mouseup','click'].forEach(function(t){ifr.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,clientX:x,clientY:y,screenX:x,screenY:y,button:0}));});})()")
        log.info("[*] dispatchEvent 点击 iframe")
        return True
    except Exception:
        return False


def get_turnstile_token(page):
    debug_turnstile_state(page, '开始')

    # ── Phase 1: auto-resolve ──
    log.info("[*] Phase 1: 等待 Turnstile 自动完成...")
    for _ in range(10):
        t = page.evaluate("(document.querySelector('[name=cf-turnstile-response]')||{}).value||''")
        if t:
            log.info("[+] Phase 1 成功")
            return t
        page.wait_for_timeout(1000)

    # ── Phase 2: inject + render ──
    log.info("[*] Phase 2: 注入 Turnstile JS API 并渲染 widget...")
    if not inject_turnstile_api(page):
        log.warning("Turnstile API 注入失败")
        return None

    wid = inject_turnstile_widget(page)
    if wid:
        log.info(f"[*] Widget 渲染成功 (id={wid}), 等待 iframe 加载 (8s)...")
        page.wait_for_timeout(8000)
        click_turnstile_iframe(page)
        t = wait_for_token(page, timeout=25)
        if t:
            log.info("[+] Phase 2 成功")
            return t
        err = page.evaluate("window.__cf_error || ''")
        if err:
            log.warning(f"Turnstile 错误: {err}")

    # ── Phase 2b: execute() ──
    log.info("[*] Phase 2b: turnstile.execute() 尝试...")
    wid2 = page.evaluate("(function(){var w=turnstile.render('#cf-turnstile-inject',{sitekey:" + json.dumps(TURNSTILE_SITEKEY) + ",callback:function(t){window.__cf_token=t;},'error-callback':function(e){window.__cf_error=e;}});setTimeout(function(){turnstile.execute(w);},500);return w;})()")
    page.wait_for_timeout(8000)
    t = wait_for_token(page, timeout=20)
    if t:
        log.info("[+] Phase 2b 成功")
        return t

    # ── Phase 4: refresh & retry ──
    log.info("[*] Phase 4: 刷新页面重试...")
    page.reload(wait_until='commit')
    page.wait_for_timeout(3000)
    debug_turnstile_state(page, '重试')

    inject_turnstile_api(page)
    wid = inject_turnstile_widget(page)
    page.wait_for_timeout(2000)
    t = wait_for_token(page, timeout=15)
    if t:
        return t

    log.warning("所有 Phase 均失败")
    debug_turnstile_state(page, '最终失败')
    return None


# ── registration flow ──

def click_signup_method(page):
    for text in ['Sign up with email', 'Email', 'sign up with email']:
        try:
            btn = page.locator(f'button:has-text("{text}")').first
            if btn.is_visible(timeout=3000):
                btn.click()
                log.info(f"[*] 已点击\"{text}\"")
                page.wait_for_timeout(2000)
                return True
        except Exception:
            continue
    # fallback: try any visible button that could be the email signup
    try:
        buttons = page.locator('button')
        count = buttons.count()
        for i in range(count):
            btn = buttons.nth(i)
            if btn.is_visible(timeout=500):
                txt = (btn.text_content() or '').lower()
                if 'email' in txt or 'sign up' in txt:
                    btn.click()
                    log.info(f"[*] 已点击 button '{txt[:30]}'")
                    page.wait_for_timeout(2000)
                    return True
    except Exception:
        pass
    return False

def fill_email_and_submit(page, email):
    log.info("[*] 填写邮箱...")

    inp = find_visible(page, [
        'input[name="email"]',
        'input[type="email"]',
        'input[autocomplete="email"]',
        'input[data-testid="email"]',
    ], timeout=5000)
    if inp:
        inp.fill(email)
        log.info(f"[*] 已填入邮箱: {email}")
    else:
        log.warning("未找到邮箱输入框")
        page.wait_for_timeout(2000)
        # retry: print all inputs for debug
        all_inputs = page.evaluate("Array.from(document.querySelectorAll('input')).map(i => i.name + '=' + i.type + ' visible=' + (i.offsetParent!==null))")
        log.debug(f"页面 inputs: {all_inputs}")
        return False

    btn = find_visible(page, [
        'button[type="submit"]',
        'button:has-text("Continue")',
        'button:has-text("Sign up")',
        'button:has-text("Next")',
    ], timeout=3000)
    if btn:
        btn_txt = (btn.text_content() or '').strip()[:30]
        is_disabled = btn.is_disabled()
        log.info(f"[*] 点击提交按钮 '{btn_txt}' (disabled={is_disabled})")
        if is_disabled:
            log.warning("提交按钮被禁用，可能需 Turnstile")
            # try JS click anyway
            page.evaluate("document.querySelector('button[type=\"submit\"]')?.click()")
        else:
            btn.click()
        page.wait_for_timeout(2000)
        page.wait_for_load_state('networkidle', timeout=10000)
        cur_url = page.url
        log.info(f"[*] 提交后 URL: {cur_url}")
        # Check for errors
        err = page.evaluate("""() => {
            const els = document.querySelectorAll('[role="alert"], .error, .message, p, span, div');
            return Array.from(els).filter(e => e.textContent.trim()).slice(0,10).map(e => e.textContent.trim().slice(0,60));
        }""")
        if err:
            log.debug(f"页面消息: {err}")
        return True
    log.warning("未找到提交按钮")
    return False


def fill_code_and_submit(page, code):
    log.info(f"[*] 填入验证码: {code}")
    inp = find_visible(page, [
        'input[name="code"]',
        'input[placeholder*="code" i]',
        'input[autocomplete="one-time-code"]',
        'input[maxlength="6"]',
    ], timeout=5000)
    if inp:
        inp.fill(code)
        page.wait_for_timeout(1000)
    else:
        log.warning("未找到验证码输入框，可能已自动跳转")
        return False

    btn = find_visible(page, [
        'button[type="submit"]',
        'button:has-text("Verify")',
        'button:has-text("Confirm")',
        'button:has-text("继续")',
    ], timeout=3000)
    if btn:
        btn.click()
        log.info("[*] 已提交验证码")
        page.wait_for_timeout(3000)
    return True


def fill_profile_and_submit(page):
    log.info("[*] 填写个人信息...")
    page.wait_for_timeout(2000)

    name_inp = find_visible(page, [
        'input[name="name"]',
        'input[placeholder*="name" i]',
        'input[autocomplete="name"]',
    ], timeout=3000)
    if name_inp:
        fn = random.choice(['Alex','Sam','Jordan','Taylor','Morgan','Casey','Riley','Quinn'])
        ln = random.choice(['Smith','Johnson','Williams','Brown','Jones','Garcia','Miller'])
        name_inp.fill(f"{fn} {ln}")
        log.info(f"[*] 已填入姓名: {fn} {ln}")

    user_inp = find_visible(page, [
        'input[name="username"]',
        'input[autocomplete="username"]',
        'input[placeholder*="username" i]',
    ], timeout=2000)
    if user_inp:
        uname = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
        user_inp.fill(uname)
        log.info(f"[*] 已填入用户名: {uname}")

    t = get_turnstile_token(page)
    if t:
        log.info(f"[+] 个人资料页 Turnstile 已解决")

    btn = find_visible(page, [
        'button[type="submit"]',
        'button:has-text("Complete sign up")',
        'button:has-text("Create account")',
        'button:has-text("完成")',
    ], timeout=3000)
    if btn:
        btn.click()
        log.info("[*] 已提交个人资料")
        page.wait_for_timeout(3000)
        return True
    log.warning("未找到个人资料提交按钮")
    return False


def extract_sso_token(page):
    return page.evaluate("""() => {
        try {
            for (let k of Object.keys(localStorage)) {
                if (k.toLowerCase().includes('token') || k.toLowerCase().includes('sso') || k.toLowerCase().includes('auth'))
                    return localStorage.getItem(k);
            }
        } catch(e) {}
        try {
            for (let k of Object.keys(sessionStorage)) {
                if (k.toLowerCase().includes('token') || k.toLowerCase().includes('sso'))
                    return sessionStorage.getItem(k);
            }
        } catch(e) {}
        const scripts = document.querySelectorAll('script');
        for (let s of scripts) {
            const m = s.textContent.match(/["'](?:access_token|token|sso_token)["']\\s*:\\s*["']([^"']+)["']/);
            if (m) return m[1];
        }
        const meta = document.querySelector('meta[name="sso-token"], meta[name="token"]');
        if (meta) return meta.content;
        return null;
    }""")


# ── entry ──

def main():
    parser = argparse.ArgumentParser(description='Grok 注册工具 (Playwright)')
    parser.add_argument('--count', type=int, default=1, help='注册轮数')
    parser.add_argument('--headless', action='store_true', help='无头模式')
    parser.add_argument('--no-stealth', action='store_true', help='禁用 playwright-stealth')
    args = parser.parse_args()

    log.info("═" * 50)
    log.info("  Grok 注册机启动 (Playwright + Stealth)")
    log.info(f"  轮数: {args.count}, stealth={'是' if HAVE_STEALTH and not args.no_stealth else '否'}")
    log.info("═" * 50)

    for rnd in range(1, args.count + 1):
        log.info(f"--- 第 {rnd}/{args.count} 轮 " + "-" * 30)

        try:
            email, dev_token = get_email_and_token()
            log.info(f"[*] 邮箱创建成功: {email}")
        except Exception as e:
            log.error(f"创建邮箱失败: {e}")
            continue

        profile_dir = os.getenv('CHROME_PROFILE_DIR', '/data/chrome-profile')
        os.makedirs(profile_dir, exist_ok=True)

        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=args.headless,
                executable_path=CHROMIUM_PATH,
                args=[
                    '--no-sandbox',
                    '--disable-blink-features=AutomationControlled',
                    '--window-size=1920,1080',
                    '--start-maximized',
                    '--disable-features=ChromeWhatsNewUI,TranslateUI',
                    '--no-first-run',
                    '--disable-sync',
                    '--disable-background-networking',
                ],
                viewport={'width': 1920, 'height': 1080},
                locale='en-US',
                timezone_id='America/New_York',
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            )

            page = context.new_page()

            # init-script stealth (always)
            page.add_init_script(STEALTH_INIT_JS)

            # playwright-stealth (optional)
            if HAVE_STEALTH and not args.no_stealth:
                try:
                    stealth_sync(page)
                    log.info("[*] playwright-stealth 已应用")
                except Exception as e:
                    log.warning(f"playwright-stealth 失败: {e}")

            log.info("[*] 访问 x.ai 注册页...")
            try:
                page.goto('https://accounts.x.ai/sign-up?redirect=grok-com', wait_until='domcontentloaded')
                page.wait_for_timeout(5000)
            except Exception as e:
                log.error(f"页面加载失败: {e}")
                browser.close()
                continue

            # 调试: 打印页面可见元素
            debug_info = page.evaluate("""() => {
                const tags = document.querySelectorAll('button, a, input, select, textarea');
                return Array.from(tags).slice(0,30).map(e => e.tagName + (e.type ? '['+e.type+']' : '') + (e.name ? ' name='+e.name : '') + ' "' + (e.textContent||'').trim().slice(0,40) + '"');
            }""")
            for line in debug_info:
                log.debug(f"  元素: {line}")

            # 点击"用邮箱注册"
            click_signup_method(page)

            # 调试: 查看点击后的页面
            after_click = page.evaluate("""() => {
                const tags = document.querySelectorAll('button, a, input');
                return Array.from(tags).slice(0,20).map(e => e.tagName + (e.type ? '['+e.type+']' : '') + (e.name ? ' name='+e.name : '') + ' visible=' + (e.offsetParent!==null) + ' "' + (e.textContent||'').trim().slice(0,30) + '"');
            }""")
            for line in after_click:
                log.debug(f"  点击后元素: {line}")

            # 填写邮箱
            if not fill_email_and_submit(page, email):
                browser.close()
                continue

            # Turnstile (在提交邮箱后处理)
            token = get_turnstile_token(page)
            if token:
                log.info(f"[+] Turnstile token: {token[:30]}...")
            else:
                log.warning("Turnstile 获取失败")

            # 等待验证码
            log.info("[*] 等待验证码...")
            code = get_oai_code(dev_token, email, timeout=120)
            if not code:
                log.error("验证码超时")
                browser.close()
                continue
            log.info(f"[*] 验证码: {code}")

            page.wait_for_timeout(3000)

            # 填入验证码
            fill_code_and_submit(page, code)

            url_after = page.url
            log.info(f"[*] 验证后 URL: {url_after}")

            # 个人资料
            if 'sign-up' in url_after or 'register' in url_after:
                fill_profile_and_submit(page)

            page.wait_for_timeout(5000)

            sso_token = extract_sso_token(page)
            if sso_token:
                log.info(f"[+] SSO token: {sso_token[:40]}...")
            else:
                log.info("[*] 未找到 SSO token")

            cookies = context.cookies()
            log.info(f"[*] Cookies: {len(cookies)} 个")
            log.info(f"[*] 最终 URL: {page.url}")

            context.close()

        log.info(f"[*] 第 {rnd} 轮完成\n")

    log.info("═" * 50)
    log.info("  所有轮次完成")
    log.info("═" * 50)


if __name__ == '__main__':
    main()
