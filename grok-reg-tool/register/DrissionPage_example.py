# -*- coding: utf-8 -*-
import sys
import os
import io

# 强制 stdout/stderr 使用 UTF-8，解决 Windows 下 WebUI 读取乱码问题
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import PageDisconnectedError
import argparse
import shutil
import tempfile
import datetime
import logging
import time
import secrets
import platform

import json as _json_mod
from email_register import get_email_and_token, get_oai_code


def setup_run_logger() -> logging.Logger:
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    # 加上 PID 避免多 worker 并发时同秒启动写到同一个日志文件
    log_path = os.path.join(log_dir, f"run_{ts}_{os.getpid()}.log")

    logger = logging.getLogger("grok_register")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.info("日志文件: %s", log_path)
    return logger


run_logger: logging.Logger = None



def ensure_stable_python_runtime():
    # 优先自动切到更稳定的 3.12 / 3.13，避免 3.14 下 Mail.tm 偶发 TLS/兼容问题。
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        print(f"[*] 检测到 Python {sys.version.split()[0]}，自动切换到更稳定的解释器: {candidate}")
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


def warn_runtime_compatibility():
    # 中文提示：避免把底层 TLS 兼容问题误判成脚本逻辑错误。
    if sys.version_info >= (3, 14):
        print("[提示] 当前 Python 为 3.14+；若出现 Mail.tm TLS 异常，建议改用 Python 3.12 或 3.13。")


ensure_stable_python_runtime()
warn_runtime_compatibility()

# 仅在 Linux 无头服务器自动启用 Xvfb 虚拟显示器
_virtual_display = None
if platform.system() == "Linux" and (not os.environ.get("DISPLAY") or os.environ.get("USE_XVFB") == "1"):
    try:
        from pyvirtualdisplay import Display
        _virtual_display = Display(visible=0, size=(1920, 1080))
        _virtual_display.start()
        print(f"[*] Xvfb 虚拟显示器已启动: {os.environ.get('DISPLAY')}")
    except Exception as e:
        print(f"[Warn] Xvfb 启动失败: {e}，将尝试直接运行")

co = ChromiumOptions()
co.auto_port()
co.set_argument("--no-sandbox")
co.set_argument("--disable-gpu")
co.set_argument("--disable-dev-shm-usage")
co.set_argument("--disable-software-rasterizer")

# 从 config.json 读取代理配置给浏览器
_browser_proxy = ""
_browser_path_cfg = ""
try:
    import json as _json_mod
    _cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.isfile(_cfg_path):
        with open(_cfg_path, "r") as _f:
            _cfg = _json_mod.load(_f)
        _browser_proxy = str(_cfg.get("browser_proxy", "") or _cfg.get("proxy", "") or "")
        _browser_path_cfg = str(_cfg.get("browser_path", "") or "")
except Exception:
    pass
if _browser_proxy:
    co.set_proxy(_browser_proxy)
    print(f"[*] 浏览器代理: {_browser_proxy}")
if _browser_path_cfg and os.path.isfile(_browser_path_cfg):
    co.set_browser_path(_browser_path_cfg)
    print(f"[*] 浏览器路径: {_browser_path_cfg}")

# Linux 服务器自动检测 chromium 路径
import platform
import shutil
import glob as _glob_mod
if platform.system() == "Linux":
    # 优先用 playwright 装的 chromium（无 AppArmor 限制）
    _pw_chromes = _glob_mod.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
    if _pw_chromes:
        co.set_browser_path(_pw_chromes[0])
    else:
        for _candidate in ["/usr/bin/chromium-browser", "/usr/bin/chromium", "/usr/bin/google-chrome"]:
            if os.path.isfile(_candidate):
                co.set_browser_path(_candidate)
                break
    # user_data_path 在 start_browser() 每轮动态设置，此处不固定

_turnstile_patch_dir = os.path.join(os.path.dirname(__file__), "turnstilePatch")
if os.path.isdir(_turnstile_patch_dir):
    _manifest_path = os.path.join(_turnstile_patch_dir, "manifest.json")
    if os.path.isfile(_manifest_path):
        co.set_argument("--load-extension=" + _turnstile_patch_dir)
        print(f"[*] 已加载 Turnstile 补丁扩展: {_turnstile_patch_dir}")

co.set_timeouts(base=1)

_chrome_temp_dir: str = ""
browser = None
page = None

SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

TURNSTILE_SITEKEY = "0x4AAAAAAAhr9JGVDZbrZOo0"

_sso_dir = os.path.join(os.path.dirname(__file__), "sso")
_sso_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
DEFAULT_SSO_FILE = os.path.join(_sso_dir, f"sso_{_sso_ts}_{os.getpid()}.txt")


def start_browser():
    # 每轮从全新浏览器开始，使用独立临时 profile 目录避免 Cookie/Session 复用。
    global browser, page, _chrome_temp_dir
    _chrome_temp_dir = tempfile.mkdtemp(prefix="chrome_run_")
    co.set_user_data_path(_chrome_temp_dir)
    browser = Chromium(co)
    tabs = browser.get_tabs()
    page = tabs[-1] if tabs else browser.new_tab()
    return browser, page


def stop_browser():
    # 完整关闭整个浏览器实例，并清理本轮临时 profile，供下一轮重新拉起。
    global browser, page, _chrome_temp_dir
    if browser is not None:
        try:
            browser.quit()
        except Exception:
            pass
    browser = None
    page = None
    if _chrome_temp_dir and os.path.isdir(_chrome_temp_dir):
        shutil.rmtree(_chrome_temp_dir, ignore_errors=True)
    _chrome_temp_dir = ""


def restart_browser():
    # 清除 cookie/storage 代替完整重启，节省 Chrome 冷启动时间。
    global browser, page
    if browser is None:
        start_browser()
        return
    try:
        tabs = browser.get_tabs()
        page = tabs[-1] if tabs else browser.new_tab()
        page.run_js("window.localStorage.clear(); window.sessionStorage.clear();")
        page.clear_cache(session_storage=True, cookies=True)
    except Exception:
        stop_browser()
        start_browser()


def refresh_active_page():
    # 验证码确认后页面会跳转，旧 page 句柄可能断开，这里统一重新获取当前活动标签页。
    global browser, page
    if browser is None:
        start_browser()
    try:
        tabs = browser.get_tabs()
        if tabs:
            page = tabs[-1]
        else:
            page = browser.new_tab()
    except Exception:
        restart_browser()
    return page


def open_signup_page():
    # 每轮开始时打开注册页，并切到“使用邮箱注册”流程。
    global page
    refresh_active_page()
    try:
        page.get(SIGNUP_URL)
    except Exception:
        refresh_active_page()
        page = browser.new_tab(SIGNUP_URL)
    click_email_signup_button()


def close_current_page():
    # 兼容旧调用名，实际行为改为整轮重启浏览器。
    restart_browser()


def has_profile_form():
    # 最终注册页只要出现姓名和密码输入框，就认为已经成功进入资料填写阶段。
    refresh_active_page()
    try:
        return bool(page.run_js(
            """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
        ))
    except Exception:
        return False


def click_email_signup_button(timeout=10):
    # 页面打开后，自动点击“使用邮箱注册”按钮。
    deadline = time.time() + timeout
    while time.time() < deadline:
        clicked = page.run_js(r"""
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = candidates.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return text.includes('使用邮箱注册') || text.includes('signupwithemail') || text.includes('signupemail') || text.includes('continuewith email') || text.includes('email');
});

if (!target) {
    return false;
}

target.click();
return true;
        """)

        if clicked:
            return True

        time.sleep(0.5)

    raise Exception('未找到“使用邮箱注册”按钮')


def fill_email_and_submit(timeout=15):
    # 复用 `email_register.py` 里的邮箱获取逻辑，保留邮箱与 token 供后续验证码步骤继续使用。
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise Exception("获取邮箱失败")

    deadline = time.time() + timeout
    while time.time() < deadline:
        filled = page.run_js(
            """
const email = arguments[0];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input) {
    return 'not-ready';
}

input.focus();
input.click();

// 不能只写 `input.value = xxx`，否则 React / 受控表单可能没有同步内部状态。
const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) {
    tracker.setValue('');
}
if (valueSetter) {
    valueSetter.call(input, email);
} else {
    input.value = email;
}

input.dispatchEvent(new InputEvent('beforeinput', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new InputEvent('input', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new Event('change', { bubbles: true }));

if ((input.value || '').trim() !== email || !input.checkValidity()) {
    return false;
}

input.blur();
return 'filled';
            """,
            email,
        )

        if filled == 'not-ready':
            time.sleep(0.5)
            continue

        if filled != 'filled':
            print(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
            time.sleep(0.5)
            continue

        if filled == 'filled':
            time.sleep(0.8)

            turnstile_token = ""
            turnstile_state = page.run_js(
                """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) {
    const ci = document.createElement('input');
    ci.type = 'hidden';
    ci.name = 'cf-turnstile-response';
    document.body.appendChild(ci);
}
const val = (challengeInput && String(challengeInput.value || '').trim()) || '';
return val ? 'ready' : 'pending';
                """
            )
            if turnstile_state == "pending":
                print("[*] 注册页存在 Turnstile，自动获取 token...")
                turnstile_token = getTurnstileToken()
                if turnstile_token:
                    _inject_turnstile_token(turnstile_token)
                    print("[+] Turnstile token 已注入邮箱注册页")

            clicked = page.run_js(
                r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input || !input.checkValidity() || !(input.value || '').trim()) {
    return false;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '注册' || text.includes('注册') || t === 'signup' || t === 'sign up' || t.includes('sign up');
});

if (!submitButton || submitButton.disabled) {
    return false;
}

submitButton.click();
return true;
                """
            )

            if clicked:
                print(f"[*] 已填写邮箱并点击注册: {email}")
                return email, dev_token

        time.sleep(0.5)

    raise Exception("未找到邮箱输入框或注册按钮")



def fill_code_and_submit(email, dev_token, timeout=60):
    # 复用 `email_register.py` 里的验证码轮询逻辑，等待邮件到达后自动填写 OTP。
    code = get_oai_code(dev_token, email)
    if not code:
        raise Exception("获取验证码失败")

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            filled = page.run_js(
                """
const code = String(arguments[0] || '').trim();

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setNativeValue(input, value) {
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }
    if (nativeInputValueSetter) {
        nativeInputValueSetter.call(input, '');
        nativeInputValueSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }
}

function dispatchInputEvents(input, value) {
    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const input = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || code.length || 6) > 1;
}) || null;

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) {
        return false;
    }
    const maxLength = Number(node.maxLength || 0);
    const autocomplete = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || autocomplete === 'one-time-code';
});

if (!input && otpBoxes.length < code.length) {
    return 'not-ready';
}

if (input) {
    input.focus();
    input.click();
    setNativeValue(input, code);
    dispatchInputEvents(input, code);

    const normalizedValue = String(input.value || '').trim();
    const expectedLength = Number(input.maxLength || code.length || 6);
    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;

    if (normalizedValue !== code) {
        return 'aggregate-mismatch';
    }

    if (expectedLength > 0 && normalizedValue.length !== expectedLength) {
        return 'aggregate-length-mismatch';
    }

    if (slots.length && filledSlots && filledSlots !== normalizedValue.length) {
        return 'aggregate-slot-mismatch';
    }

    input.blur();
    return 'filled';
}

const orderedBoxes = otpBoxes.slice(0, code.length);
for (let i = 0; i < orderedBoxes.length; i += 1) {
    const box = orderedBoxes[i];
    const char = code[i] || '';
    box.focus();
    box.click();
    setNativeValue(box, char);
    dispatchInputEvents(box, char);
    box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: char }));
    box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: char }));
    box.blur();
}

const merged = orderedBoxes.map((node) => String(node.value || '').trim()).join('');
return merged === code ? 'filled' : 'box-mismatch';
                """,
                code,
            )
        except PageDisconnectedError:
            # 点击确认邮箱后如果刚好发生跳转，旧页面句柄会断开；此时切到新页继续判断即可。
            refresh_active_page()
            if has_profile_form():
                print("[*] 验证码提交后已跳转到最终注册页。")
                return code
            time.sleep(1)
            continue

        if filled == 'not-ready':
            if has_profile_form():
                print("[*] 已直接进入最终注册页，跳过验证码按钮确认。")
                return code
            time.sleep(0.5)
            continue

        if filled != 'filled':
            print(f"[Debug] 验证码输入框已出现，但写入失败: {filled}")
            time.sleep(0.5)
            continue

        if filled == 'filled':
            time.sleep(1.2)
            try:
                clicked = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const aggregateInput = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 0) > 1;
}) || null;

let value = '';
if (aggregateInput) {
    value = String(aggregateInput.value || '').trim();
    const expectedLength = Number(aggregateInput.maxLength || value.length || 6);
    if (!value || (expectedLength > 0 && value.length !== expectedLength)) {
        return false;
    }

    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    if (slots.length) {
        const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;
        if (filledSlots && filledSlots !== value.length) {
            return false;
        }
    }
} else {
    const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
        if (!isVisible(node) || node.disabled || node.readOnly) {
            return false;
        }
        const maxLength = Number(node.maxLength || 0);
        const autocomplete = String(node.autocomplete || '').toLowerCase();
        return maxLength === 1 || autocomplete === 'one-time-code';
    });
    value = otpBoxes.map((node) => String(node.value || '').trim()).join('');
    if (!value || value.length < 6) {
        return false;
    }
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const confirmButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '确认邮箱' || text.includes('确认邮箱') || text === '继续' || text.includes('继续') || text === '下一步' || text.includes('下一步') || t.includes('confirm') || t.includes('continue') || t.includes('next') || t.includes('verify');
});

if (!confirmButton) {
    return 'no-button';
}

confirmButton.focus();
confirmButton.click();
return 'clicked';
                    """
                )
            except PageDisconnectedError:
                refresh_active_page()
                if has_profile_form():
                    print("[*] 确认邮箱后页面跳转成功，已进入最终注册页。")
                    return code
                clicked = 'disconnected'

            if clicked == 'clicked':
                print(f"[*] 已填写验证码并点击确认邮箱: {code}")
                time.sleep(2)
                refresh_active_page()
                if has_profile_form():
                    print("[*] 验证码确认完成，最终注册页已就绪。")
                return code

            if clicked == 'no-button':
                current_url = page.url
                if 'sign-up' in current_url or 'signup' in current_url:
                    print(f"[*] 已填写验证码，页面已自动跳转到下一步: {current_url}")
                    return code

            if clicked == 'disconnected':
                time.sleep(1)
                continue

        time.sleep(0.5)

    debug_snapshot = page.run_js(
        r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const inputs = Array.from(document.querySelectorAll('input')).filter(isVisible).map((node) => ({
    type: node.type || '',
    name: node.name || '',
    testid: node.getAttribute('data-testid') || '',
    autocomplete: node.autocomplete || '',
    maxLength: Number(node.maxLength || 0),
    value: String(node.value || ''),
}));

const buttons = Array.from(document.querySelectorAll('button')).filter(isVisible).map((node) => ({
    text: String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim(),
    disabled: !!node.disabled,
    ariaDisabled: node.getAttribute('aria-disabled') || '',
}));

return { url: location.href, inputs, buttons };
        """
    )
    print(f"[Debug] 验证码页 DOM 摘要: {debug_snapshot}")
    raise Exception("未找到验证码输入框或确认邮箱按钮")


def _debug_page_state(label: str = ""):
    """捕获当前页面状态用于调试。"""
    try:
        state = page.run_js(r"""
const state = {
    url: location.href,
    title: document.title,
    turnstileExists: typeof turnstile !== 'undefined',
    turnstileWidgets: 0,
    iframes: [],
    cfResponse: '',
    buttons: [],
    inputs: [],
    visibleText: '',
};
try {
    const widgets = document.querySelectorAll('[class*="turnstile"], [data-sitekey]');
    state.turnstileWidgets = widgets.length;
} catch(e) {}
try {
    state.iframes = Array.from(document.querySelectorAll('iframe')).map(f => ({
        src: (f.src || '').slice(0, 100),
        id: f.id || '',
    }));
} catch(e) {}
try {
    const inp = document.querySelector('input[name="cf-turnstile-response"]');
    if (inp) state.cfResponse = (inp.value || '').slice(0, 50);
    else state.cfResponse = '__not_found__';
} catch(e) {}
try {
    state.buttons = Array.from(document.querySelectorAll('button')).filter(b => {
        const s = getComputedStyle(b);
        return s.display !== 'none' && s.visibility !== 'hidden';
    }).map(b => ({
        text: (b.innerText || '').slice(0, 40),
        disabled: b.disabled,
    }));
} catch(e) {}
try {
    state.visibleText = (document.body && document.body.innerText || '').slice(0, 300);
} catch(e) {}
return state;
        """)
        if state:
            print(f"[Debug] 页面状态 ({label}): URL={state.get('url','')[:80]}")
            turnstile_ok = state.get('turnstileExists')
            widgets = state.get('turnstileWidgets', 0)
            cf = state.get('cfResponse', '')
            print(f"[Debug]   turnstile对象={'是' if turnstile_ok else '否'}, widgets={widgets}, cf-response={cf}")
            if state.get('iframes'):
                for f in state['iframes']:
                    print(f"[Debug]   iframe: {f.get('src','')[:80]}")
            if state.get('buttons'):
                for b in state['buttons']:
                    status = '✓可用' if not b.get('disabled') else '✗禁用'
                    print(f"[Debug]   按钮 [{status}]: {b.get('text','')}")
    except Exception as e:
        print(f"[Debug] 页面状态捕获失败: {e}")


def _wait_for_turnstile_iframe(timeout: float = 30) -> bool:
    """等待 Turnstile iframe 出现（DOM 就绪），超时返回 False。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = page.run_js(
            r"""
return !!document.querySelector('.cf-turnstile, .cf-turnstile-wrapper, [data-cf-turnstile], iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]');
            """
        )
        if found:
            return True
        time.sleep(0.3)
    return False


def _get_turnstile_response() -> str:
    """读取 Turnstile challenge input 或 JS API 返回的 token。"""
    try:
        resp = page.run_js(
            r"""
// 1. 直接从 hidden input 读取（Turnstile 自动填充）
const input = document.querySelector('input[name="cf-turnstile-response"]');
if (input && String(input.value || '').trim()) {
    return String(input.value || '').trim();
}
// 2. 通过 Cloudflare Turnstile JS API 获取
try {
    const token = (window.turnstile && turnstile.getResponse()) || '';
    if (token) return token;
} catch (e) {}
// 3. 查找所有 widget 实例
try {
    if (window.turnstile) {
        const widgets = document.querySelectorAll('[class*="turnstile"]');
        for (const w of widgets) {
            const id = w.getAttribute('data-widget-id') || w.id;
            if (id) {
                const t = turnstile.getResponse(id);
                if (t) return t;
            }
        }
    }
} catch(e) {}
return '';
            """
        )
        return (resp or "").strip()
    except Exception:
        return ""


def _try_turnstile_execute(timeout: int = 30) -> str:
    """程序化触发 turnstile.execute() 并等待 token。"""
    result = page.run_js(
        r"""
const timeout = arguments[0];
return new Promise((resolve) => {
    if (typeof turnstile === 'undefined') {
        resolve('__no_turnstile__');
        return;
    }
    // 获取当前可渲染的 widget 元素
    const containers = document.querySelectorAll('.cf-turnstile, [data-sitekey], [data-cf-turnstile]');
    let widgetId = null;
    
    // 如果已经有渲染的 widget，获取它的 widgetId
    for (const c of containers) {
        const id = c.getAttribute('data-widget-id');
        if (id) { widgetId = id; break; }
    }
    
    if (widgetId) {
        // 存在已渲染的 widget，执行它
        turnstile.execute(widgetId, {
            callback: (token) => resolve(token),
        });
        setTimeout(() => resolve('__timeout__'), timeout * 1000);
    } else if (containers.length > 0) {
        // 有容器但未渲染，渲染后再执行
        const container = containers[0];
        const sitekey = container.getAttribute('data-sitekey') || '';
        if (!sitekey) {
            resolve('__no_sitekey__');
            return;
        }
        turnstile.render(container, {
            sitekey: sitekey,
            callback: (token) => resolve(token),
        });
        setTimeout(() => resolve('__timeout__'), timeout * 1000);
    } else {
        resolve('__no_container__');
    }
});
        """,
        timeout,
    )
    if result and not str(result).startswith("__"):
        return str(result)
    return ""


def _inject_turnstile_token(token: str) -> bool:
    """将 Turnstile token 注入到隐藏 input 并触发事件。"""
    if not token:
        return False
    try:
        page.run_js(
            r"""
const token = arguments[0];
const input = document.querySelector('input[name="cf-turnstile-response"]');
if (!input) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (nativeSetter) {
    nativeSetter.call(input, token);
} else {
    input.value = token;
}
input.dispatchEvent(new Event('change', { bubbles: true }));
input.dispatchEvent(new Event('input', { bubbles: true }));
// 也尝试触发 submit 表单的 turnstile 回调
try {
    const form = input.closest('form');
    if (form) form.dispatchEvent(new Event('turnstile-callback', { bubbles: true }));
} catch(e) {}
return String(input.value || '') === token;
            """
        )
        return True
    except Exception:
        return False


def _extract_sitekey_aggressive() -> str:
    """从页面多维度提取 Turnstile sitekey。"""
    return page.run_js(r"""
function walkShadowRoots(root) {
    if (!root) return null;
    // 查询当前 root
    const containers = root.querySelectorAll('.cf-turnstile, .cf-turnstile-wrapper, [data-cf-turnstile], [data-sitekey]');
    for (const c of containers) {
        const key = c.getAttribute('data-sitekey');
        if (key) return key;
    }
    // 遍历 shadow DOM
    const all = root.querySelectorAll('*');
    for (const el of all) {
        if (el.shadowRoot) {
            const result = walkShadowRoots(el.shadowRoot);
            if (result) return result;
        }
    }
    return null;
}

function extract() {
    // 1. 标准 class / data-sitekey（含 shadow DOM）
    const shadowResult = walkShadowRoots(document);
    if (shadowResult) return shadowResult;

    // 2. 查找 iframe 的 src 参数
    const iframes = document.querySelectorAll('iframe[src*="turnstile"], iframe[src*="challenges.cloudflare"]');
    for (const f of iframes) {
        const src = f.src || '';
        const m = src.match(/[?&]sitekey=([^&]+)/);
        if (m) return m[1];
    }
    // 3. 查找所有 script 标签中的 sitekey 变量
    const scripts = document.querySelectorAll('script');
    for (const s of scripts) {
        const text = s.textContent || '';
        const m = text.match(/['"]sitekey['"]\s*[:=]\s*['"]([^'"]+)['"]/);
        if (m) return m[1];
        const m2 = text.match(/turnstile\.render\s*\([^)]*['"]?sitekey['"]?\s*[:=]\s*['"]([^'"]+)['"]/);
        if (m2) return m2[1];
    }
    // 4. 查找全局 turnstile 配置
    try {
        if (window.turnstile) {
            if (turnstile._cf && turnstile._cf.key) return turnstile._cf.key;
        }
    } catch(e) {}
    // 5. 正则全局扫描 HTML
    try {
        const html = document.documentElement.outerHTML;
        const m = html.match(/data-sitekey=["']([^"']+)["']/);
        if (m) return m[1];
        const m2 = html.match(/["']sitekey["']\s*[:=]\s*["']([^"']+)["']/);
        if (m2) return m2[1];
    } catch(e) {}
    return '';
}
return extract();
    """)


def _ensure_turnstile_loaded(max_wait: float = 15) -> bool:
    """确保 Turnstile JS API 已加载到页面，若未加载则动态注入。"""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        loaded = page.run_js("return typeof turnstile !== 'undefined';")
        if loaded:
            return True
        page.run_js(r"""
if (document.querySelector('script[src*="challenges.cloudflare.com/turnstile"]')) return;
const s = document.createElement('script');
s.src = 'https://challenges.cloudflare.com/turnstile/v0/g/api.js?render=explicit';
s.async = true;
s.defer = true;
document.head.appendChild(s);
        """)
        time.sleep(2)
    return bool(page.run_js("return typeof turnstile !== 'undefined';"))


def _inject_turnstile_widget(sitekey: str = TURNSTILE_SITEKEY, container_id: str = "cf-turnstile-inject") -> str:
    """在页面中注入一个 Turnstile widget 并返回 widget ID（空串表示失败）。"""
    return page.run_js(r"""
const sitekey = arguments[0];
const containerId = arguments[1];
try {
    if (typeof turnstile === 'undefined') return '__no_turnstile__';
    let container = document.getElementById(containerId);
    if (!container) {
        container = document.createElement('div');
        container.id = containerId;
        container.style.width = '0';
        container.style.height = '0';
        container.style.overflow = 'hidden';
        container.style.position = 'absolute';
        container.style.opacity = '0';
        container.style.pointerEvents = 'none';
        document.body.appendChild(container);
    }
    const existingInput = document.querySelector('input[name="cf-turnstile-response"]');
    if (existingInput && String(existingInput.value || '').trim()) {
        return '__already_done__';
    }
    const wid = turnstile.render(containerId, {
        sitekey: sitekey,
        callback: function(token) {
            const inp = document.querySelector('input[name="cf-turnstile-response"]');
            if (inp) {
                const ns = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                if (ns) { ns.call(inp, token); }
                else { inp.value = token; }
                inp.dispatchEvent(new Event('change', { bubbles: true }));
                inp.dispatchEvent(new Event('input', { bubbles: true }));
            }
            window.__cf_turnstile_token = token;
        },
    });
    return String(wid);
} catch(e) {
    return '__error__:' + String(e.message || e);
}
    """, sitekey, container_id)


def _wait_for_turnstile_token_from_widget(timeout: float = 45) -> str:
    """等待注入的 widget 通过回调写入 token（轮询 cf-turnstile-response / window.__cf_turnstile_token）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        token = page.run_js(r"""
const inp = document.querySelector('input[name="cf-turnstile-response"]');
const v = (inp && String(inp.value || '').trim()) || window.__cf_turnstile_token || '';
return v;
        """)
        if token:
            return token
        time.sleep(0.5)
    return ""


def _click_turnstile_iframe_checkbox(timeout: float = 30) -> bool:
    """在页面中找到 Turnstile iframe 复选框并点击它以触发挑战。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _wait_for_turnstile_iframe(timeout=3):
            clicked = page.run_js(r"""
const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"], iframe[title*="challenge"], iframe[src*="turnstile"]');
if (!iframe) return false;
try {
    const rect = iframe.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    iframe.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: x, clientY: y, screenX: x + 100, screenY: y + 100 }));
    iframe.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: x, clientY: y, screenX: x + 100, screenY: y + 100 }));
    iframe.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: x, clientY: y, screenX: x + 100, screenY: y + 100 }));
    return true;
} catch(e) { return false; }
            """)
            if clicked:
                print("[*] Turnstile iframe 复选框已点击，等待挑战完成...")
                return True
        time.sleep(1)
    return False


def getTurnstileToken() -> str:
    """自动获取 Turnstile token（全自动，无需人工干预）：

    策略：
    Phase 1: 页面已存在 Turnstile，等待 iframe 自动完成（30s）
    Phase 2: 注入 Turnstile JS API + 用已知 sitekey 渲染 widget 并等待 token
    Phase 3: curl_cffi 补充（失败率较高）
    Phase 4: 刷新页面重试一次
    """
    _debug_page_state("Turnstile 开始")

    # Phase 1: 先检查是否已有 token
    token = _get_turnstile_response()
    if token:
        print(f"[+] 页面已有 Turnstile token")
        return token

    # Phase 1b: 等待 iframe 出现并自动完成
    print("[*] Phase 1: 等待 Turnstile iframe 自动完成...")
    if _wait_for_turnstile_iframe(timeout=30):
        token = _get_turnstile_response()
        if token:
            print(f"[+] Turnstile 自动完成")
            return token

    # Phase 2: 注入/确保 Turnstile JS API，用已知 sitekey 渲染 widget
    print("[*] Phase 2: 注入 Turnstile JS API 并渲染 widget...")
    _ensure_turnstile_loaded()
    wid = _inject_turnstile_widget()
    if wid and not str(wid).startswith("__"):
        print(f"[*] Turnstile widget 已渲染 (id={wid[:20]}), 等待 token...")
        token = _wait_for_turnstile_token_from_widget(timeout=45)
        if token:
            print(f"[+] 注入 widget 方式获取 token 成功")
            return token
    else:
        print(f"[Warn] 注入 widget 结果: {wid}")

    # Phase 2b: 尝试 turnstile.execute() 程序化触发
    print("[*] Phase 2b: 尝试 turnstile.execute() 程序化触发...")
    token = _try_turnstile_execute(timeout=20)
    if token:
        print(f"[+] turnstile.execute() 成功获取 token")
        return token

    # Phase 3: curl_cffi API 调用（用硬编码的已知 sitekey）
    print("[*] Phase 3: 尝试 curl_cffi Turnstile API（已知 sitekey）...")
    token = _get_turnstile_via_cffi(sitekey=TURNSTILE_SITEKEY)
    if token:
        return token

    # Phase 4: 刷新页面再试一次
    print("[*] Phase 4: 刷新页面后重试...")
    refresh_active_page()
    time.sleep(5)
    _debug_page_state("Turnstile 重试")
    _ensure_turnstile_loaded()
    wid = _inject_turnstile_widget()
    if wid and not str(wid).startswith("__"):
        token = _wait_for_turnstile_token_from_widget(timeout=45)
        if token:
            print(f"[+] 刷新后 widget 方式获取 token 成功")
            return token
    if _wait_for_turnstile_iframe(timeout=15):
        token = _get_turnstile_response()
        if token:
            return token
    token = _get_turnstile_via_cffi(sitekey=TURNSTILE_SITEKEY)
    if token:
        return token

    _debug_page_state("Turnstile 最终失败")
    print("[Warn] 所有 Turnstile 获取方式均失败，尝试直接提交")
    return ""


def _get_turnstile_via_cffi(sitekey: str = None) -> str:
    """用 curl_cffi 模拟 Cloudflare Turnstile API 调用获取 token。

    流程：
    1. 从页面提取 sitekey（或使用传入的已知值）
    2. 通过 actions.cloudflare.com/api/v4/c/create 获取 challenge data
    3. 通过 actions.cloudflare.com/api/v4/c/challenge 获取 token
    """
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        print("[Warn] curl_cffi 未安装，跳过 API 获取")
        return ""

    # Step 1: 获取 sitekey
    if not sitekey:
        sitekey = _extract_sitekey_aggressive()
    if not sitekey:
        print("[Warn] 未找到 Turnstile sitekey")
        return ""
    print(f"[*] 使用 sitekey: {sitekey[:20]}...")

    # Step 2: 创建 challenge
    session = cffi_requests.Session(impersonate="chrome131")
    try:
        cf_token_url = page.run_js(r"""
const script = document.querySelector('script[src*="challenges.cloudflare.com"]');
if (script) {
    const src = script.src || '';
    const m = src.match(/(https:\/\/challenges\.cloudflare\.com\/token\/v[0-9]+\?c=[^&"]+)/);
    if (m) return m[1];
}
return '';
        """)

        if not cf_token_url:
            cf_token_url = "https://challenges.cloudflare.com/token/v1"
            print(f"[*] 使用默认 cf-token-url")

        challenge_resp = session.post(
            cf_token_url,
            json={
                "action": "sign-up",
                "cData": "",
                "reason": "onload",
                "sitekey": sitekey,
            },
            timeout=15,
            allow_redirects=True,
        )

        if challenge_resp.status_code != 200:
            print(f"[Warn] Challenge 创建失败: HTTP {challenge_resp.status_code}")
            return ""

        challenge_data = challenge_resp.json()
        challenge_id = challenge_data.get("challenge")
        w_url = challenge_data.get("wUrl")

        if not challenge_id or not w_url:
            print(f"[Warn] Challenge 响应缺少 challenge/wUrl: {challenge_data}")
            return ""

        print(f"[*] Challenge 创建成功，开始获取 token...")

        token_session = cffi_requests.Session(impersonate="chrome131")
        deadline = time.time() + 60
        poll_count = 0
        while time.time() < deadline:
            poll_count += 1
            token_resp = token_session.get(
                f"{w_url}/api/v4/c/{challenge_id}",
                params={"lang": "en"},
                timeout=10,
            )
            if token_resp.status_code == 200:
                token_data = token_resp.json()
                token_value = token_data.get("token") or token_data.get("response")
                if token_value:
                    print(f"[+] Turnstile token 获取成功（第 {poll_count} 轮）")
                    return token_value
            time.sleep(1)

        print(f"[Warn] Turnstile API 轮询超时（{poll_count} 轮）")
        return ""

    except Exception as e:
        print(f"[Warn] curl_cffi Turnstile API 获取失败: {e}")
        return ""


_GIVEN_NAMES = [
    "Aaron", "Adam", "Adrian", "Alan", "Albert", "Alex", "Alice", "Allen",
    "Amy", "Andrew", "Angela", "Anna", "Anthony", "Ashley", "Austin", "Bella",
    "Benjamin", "Bradley", "Brandon", "Brian", "Caleb", "Cameron", "Carl",
    "Carol", "Charles", "Chloe", "Chris", "Claire", "Cody", "Connor", "Daniel",
    "David", "Dean", "Dennis", "Derek", "Diana", "Donald", "Doris", "Douglas",
    "Dylan", "Edward", "Elaine", "Eli", "Elijah", "Ella", "Emily", "Eric",
    "Ethan", "Eva", "Evan", "Felix", "Frank", "Gabriel", "Gary", "George",
    "Grace", "Grant", "Gregory", "Hannah", "Harold", "Harry", "Henry", "Ian",
    "Isaac", "Ivan", "Jack", "Jacob", "James", "Jane", "Jason", "Jay",
    "Jeffrey", "Jennifer", "Jeremy", "Jessica", "John", "Jonathan", "Jordan",
    "Joseph", "Joshua", "Julia", "Justin", "Karen", "Kate", "Keith", "Kelly",
    "Kenneth", "Kevin", "Kyle", "Larry", "Laura", "Lauren", "Leah", "Lee",
    "Leo", "Linda", "Logan", "Louis", "Lucas", "Lucy", "Luke", "Mark",
    "Martin", "Mary", "Mason", "Matthew", "Megan", "Melissa", "Michael",
    "Mike", "Nancy", "Nathan", "Neo", "Nicholas", "Noah", "Olivia", "Oscar",
    "Owen", "Patrick", "Paul", "Peter", "Philip", "Rachel", "Ralph", "Randy",
    "Ray", "Rebecca", "Richard", "Robert", "Roger", "Ronald", "Rose", "Russell",
    "Ryan", "Samantha", "Samuel", "Sandra", "Sarah", "Scott", "Sean", "Sharon",
    "Shawn", "Sophia", "Stanley", "Stephen", "Steven", "Susan", "Thomas",
    "Tim", "Travis", "Tyler", "Victor", "Victoria", "Vincent", "Walter",
    "Wayne", "William", "Wyatt", "Zachary", "Zoey",
]

_FAMILY_NAMES = [
    "Adams", "Allen", "Anderson", "Bailey", "Baker", "Barnes", "Bell",
    "Bennett", "Brooks", "Brown", "Bryant", "Butler", "Campbell", "Carter",
    "Chen", "Clark", "Coleman", "Collins", "Cook", "Cooper", "Cox", "Cruz",
    "Davis", "Diaz", "Edwards", "Evans", "Fisher", "Flores", "Foster",
    "Garcia", "Gomez", "Gonzalez", "Gray", "Green", "Hall", "Harris",
    "Hayes", "Henderson", "Hernandez", "Hill", "Holmes", "Howard", "Hughes",
    "Hunter", "Jackson", "James", "Jenkins", "Johnson", "Jones", "Kelly",
    "Khan", "Kim", "King", "Lee", "Lewis", "Lin", "Long", "Lopez", "Martin",
    "Martinez", "Miller", "Mitchell", "Moore", "Morales", "Morgan", "Morris",
    "Murphy", "Murray", "Nelson", "Nguyen", "Owens", "Parker", "Patel",
    "Perez", "Peterson", "Phillips", "Powell", "Price", "Ramirez", "Reed",
    "Reyes", "Richardson", "Rivera", "Roberts", "Robinson", "Rodriguez",
    "Rogers", "Ross", "Russell", "Sanchez", "Sanders", "Scott", "Simmons",
    "Smith", "Stewart", "Sullivan", "Taylor", "Thomas", "Thompson", "Torres",
    "Turner", "Walker", "Wang", "Ward", "Watson", "White", "Williams",
    "Wilson", "Wood", "Wright", "Young", "Zhang", "Zhou",
]


def build_profile():
    # 生成一组可重复使用的注册资料，姓名从英文常见姓名表里随机抽取，
    # 密码至少包含大小写、数字和特殊字符。
    given_name = secrets.choice(_GIVEN_NAMES)
    family_name = secrets.choice(_FAMILY_NAMES)
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password


def fill_profile_and_submit(timeout=360):
    # 在验证码通过后，直接锁定“可见且可写”的真实输入框，避免命中隐藏节点或 React 受控副本。
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    turnstile_token = ""

    while time.time() < deadline:
        filled = page.run_js(
            """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) {
        return false;
    }
    input.focus();
    input.click();

    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }

    if (nativeSetter) {
        nativeSetter.call(input, '');
        nativeSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }

    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.dispatchEvent(new Event('blur', { bubbles: true }));

    return String(input.value || '') === String(value || '');
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) {
    return 'not-ready';
}

const givenOk = setInputValue(givenInput, givenName);
const familyOk = setInputValue(familyInput, familyName);
const passwordOk = setInputValue(passwordInput, password);

if (!givenOk || !familyOk || !passwordOk) {
    return 'filled-failed';
}

return [
    String(givenInput.value || '').trim() === String(givenName || '').trim(),
    String(familyInput.value || '').trim() === String(familyName || '').trim(),
    String(passwordInput.value || '') === String(password || ''),
].every(Boolean) ? 'filled' : 'verify-failed';
            """,
            given_name,
            family_name,
            password,
        )

        if filled == 'not-ready':
            time.sleep(0.5)
            continue

        if filled != 'filled':
            print(f"[Debug] 最终注册页输入框已出现，但姓名/密码写入失败: {filled}")
            time.sleep(0.5)
            continue

        values_ok = page.run_js(
            """
const expectedGiven = arguments[0];
const expectedFamily = arguments[1];
const expectedPassword = arguments[2];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) {
    return false;
}

return String(givenInput.value || '').trim() === String(expectedGiven || '').trim()
    && String(familyInput.value || '').trim() === String(expectedFamily || '').trim()
    && String(passwordInput.value || '') === String(expectedPassword || '');
            """,
            given_name,
            family_name,
            password,
        )
        if not values_ok:
            print("[Debug] 最终注册页字段值校验失败，继续重试填写。")
            time.sleep(0.5)
            continue

        turnstile_state = page.run_js(
            """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) {
    return 'not-found';
}
const value = String(challengeInput.value || '').trim();
return value ? 'ready' : 'pending';
            """
        )

        if turnstile_state == "pending" and not turnstile_token:
            print("[*] 检测到最终注册页存在 Turnstile，自动获取 token...")
            turnstile_token = getTurnstileToken()
            if turnstile_token:
                print("[+] Turnstile token 获取成功，注入到表单...")
                _inject_turnstile_token(turnstile_token)
            else:
                print("[Warn] Turnstile token 获取失败，尝试直接提交")
        elif turnstile_state == "not-found":
            print("[*] 最终注册页未发现 Turnstile 输入框，尝试确保 Turnstile API 已加载...")
            if _ensure_turnstile_loaded():
                _inject_turnstile_widget()
                token = _wait_for_turnstile_token_from_widget(timeout=30)
                if token:
                    print("[+] 注入 widget 成功获取 Turnstile token")
                    _inject_turnstile_token(token)
                    turnstile_token = token

        time.sleep(1)

        try:
            submit_button = page.ele('tag:button@@text()=完成注册') or page.ele('tag:button@@text():Create Account') or page.ele('tag:button@@text():Sign up')
        except Exception:
            submit_button = None

        if not submit_button:
            clicked = page.run_js(
                r"""
function isVisible(node) {
    if (!node) return false;
    const s = window.getComputedStyle(node);
    if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
    const r = node.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button'));
const submitButton = buttons.find((node) => {
    if (!isVisible(node) || node.disabled || node.getAttribute('aria-disabled') === 'true') return false;
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase();
    return text === '完成注册' || text.includes('完成注册') || t.includes('create account') || t.includes('sign up') || t.includes('complete');
});
if (!submitButton) return false;
submitButton.focus();
submitButton.click();
return true;
                """
            )
        else:
            try:
                submit_button.click()
                clicked = True
            except Exception:
                clicked = False

        if clicked:
            print(f"[*] 已填写注册资料并点击完成注册: {given_name} {family_name} / {password}")
            time.sleep(3)
            current_url = ""
            try:
                current_url = page.url or ""
            except Exception:
                pass
            if current_url and "sign-up" in current_url:
                error_text = page.run_js(r"""
try {
    const errEls = document.querySelectorAll('[role="alert"], .error, .error-message, [data-error], .text-red-500, .text-error');
    return Array.from(errEls).map(e => (e.textContent || '').trim()).filter(Boolean).join(' | ');
} catch(e) { return ''; }
                """)
                if error_text:
                    print(f"[Warn] 提交后页面显示错误: {error_text}")
                _debug_page_state("提交后仍停留在注册页")
            return {
                "given_name": given_name,
                "family_name": family_name,
                "password": password,
            }

        time.sleep(0.5)

    _debug_page_state("fill_profile 最终失败")
    raise Exception("未找到最终注册表单或完成注册按钮")


def extract_visible_numbers(timeout=60):
    # 登录/注册完成后，提取页面上可见的普通数字文本，不处理任何敏感 Cookie。
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = page.run_js(
            r"""
function isVisible(el) {
    if (!el) {
        return false;
    }
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const selector = [
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'div', 'span', 'p', 'strong', 'b', 'small',
    '[data-testid]', '[class]', '[role="heading"]'
].join(',');

const seen = new Set();
const matches = [];
for (const node of document.querySelectorAll(selector)) {
    if (!isVisible(node)) {
        continue;
    }
    const text = String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
    if (!text) {
        continue;
    }
    const found = text.match(/\d+(?:\.\d+)?/g);
    if (!found) {
        continue;
    }
    for (const value of found) {
        const key = `${value}@@${text}`;
        if (seen.has(key)) {
            continue;
        }
        seen.add(key);
        matches.push({ value, text });
    }
}

return matches.slice(0, 30);
            """
        )

        if result:
            print("[*] 页面可见数字文本提取结果:")
            for item in result:
                try:
                    print(f"    - 数字: {item['value']} | 上下文: {item['text']}")
                except Exception:
                    pass
            return result

        time.sleep(1)

    raise Exception("登录后未提取到可见数字文本")


def wait_for_sso_cookie(timeout=30, prefer_domain: str = "grok.com"):
    # 必须在注册完成后再取 sso，优先抓取 grok.com 域上的 sso 值。
    # 历史背景：accounts.x.ai 域和 grok.com 域上都会出现一个名为 "sso" 的 cookie；
    # grok2api 真正要用的是 grok.com 那一份（和 chat 接口同域），如果错拿了
    # accounts.x.ai 那一份，下游调用会被风控秒拒。
    deadline = time.time() + timeout
    last_seen_names = set()
    fallback_value = ""  # 拿不到 prefer_domain 上的，再退回任意域的 sso

    def _scan_cookies(cookie_iter):
        nonlocal fallback_value
        for item in cookie_iter:
            if isinstance(item, dict):
                name = str(item.get("name", "")).strip()
                value = str(item.get("value", "")).strip()
                domain = str(item.get("domain", "")).strip().lstrip(".")
            else:
                name = str(getattr(item, "name", "")).strip()
                value = str(getattr(item, "value", "")).strip()
                domain = str(getattr(item, "domain", "")).strip().lstrip(".")
            if name:
                last_seen_names.add(f"{name}@{domain}" if domain else name)
            if name == "sso" and value:
                if prefer_domain and prefer_domain in domain:
                    return ("preferred", domain, value)
                if not fallback_value:
                    fallback_value = value
        return None

    while time.time() < deadline:
        try:
            # 不依赖单一 page 句柄——warm-up 期间 grok.com 里的 turnstile/广告 iframe
            # 可能让 page 飘到不相关的标签页（比如 NID@google.com）。所以我们扫所有标签页：
            # 优先在显式访问 grok.com 的标签页里找 sso；找不到再退回当前 page。
            grok_tab = None
            try:
                if browser is not None:
                    for tab in browser.get_tabs():
                        try:
                            url = (tab.url or "")
                        except Exception:
                            url = ""
                        if "grok.com" in url:
                            grok_tab = tab
                            break
            except Exception:
                grok_tab = None

            target = grok_tab or page
            if target is None:
                time.sleep(1)
                continue

            cookies = target.cookies(all_domains=True, all_info=True) or []
            hit = _scan_cookies(cookies)
            if hit:
                _, domain, value = hit
                print(f"[*] 已获取到 {domain} 域的 sso cookie。")
                return value

        except PageDisconnectedError:
            refresh_active_page()
        except Exception:
            pass

        time.sleep(1)

    if fallback_value:
        print(f"[Warn] 未拿到 {prefer_domain} 域的 sso，退回到非首选域的 sso（可能仍能用）。")
        return fallback_value

    raise Exception(f"注册完成后未获取到 sso cookie，当前已见 cookie: {sorted(last_seen_names)}")


def wait_for_grok_com_landing(timeout: int = 90) -> bool:
    # 注册流（accounts.x.ai/sign-up?redirect=grok-com）完成后，浏览器会经过一段
    # SSO 重定向链，最终落到 grok.com 并把会话 cookie 写到 grok.com 域上。
    # grok.com 是独立域，跟 .x.ai 不共享 cookie。
    # 之前的版本在重定向链跑完之前就已经在 wait_for_sso_cookie 拿到 accounts.x.ai 的
    # sso 抢跑返回，warm-up 接着用硬跳 (page.get) 去 grok.com，结果落在未登录状态。
    # 这里显式等到 URL 真正变成 grok.com 且页面进入登录态再返回。
    global page
    deadline = time.time() + timeout
    last_url = ""
    while time.time() < deadline:
        try:
            refresh_active_page()
            current_url = page.url or ""
            if current_url != last_url:
                print(f"[*] 等待重定向到 grok.com，当前: {current_url}")
                last_url = current_url

            if "grok.com" in current_url:
                logged_in = bool(page.run_js(r"""
function isVisible(n) {
    if (!n) return false;
    const s = window.getComputedStyle(n);
    if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
    const r = n.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
}
// 已进入 chat 路径 = 必然已登录
if (/grok\.com\/(chat|c)\//.test(location.href)) return true;
// 输入框出现 = 已登录
const ta = Array.from(document.querySelectorAll('textarea, [contenteditable="true"]')).find(n => isVisible(n) && !n.disabled && !n.readOnly);
if (ta) return true;
return false;
"""))
                if logged_in:
                    print(f"[*] 已落到 grok.com 并登录: {current_url}")
                    return True
        except PageDisconnectedError:
            refresh_active_page()
        except Exception:
            pass
        time.sleep(1)

    print(f"[Warn] 等待 grok.com 登录超时，最后 URL: {last_url}")
    return False


def append_sso_to_txt(sso_value, output_path=DEFAULT_SSO_FILE):
    # 按用户要求，一行写一个 sso 值，持续追加。
    normalized = str(sso_value or "").strip()
    if not normalized:
        raise Exception("待写入的 sso 为空")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "a", encoding="utf-8") as file:
        file.write(normalized + "\n")

    print(f"[*] 已追加写入 sso 到文件: {output_path}")


def push_sso_to_api(new_tokens: list):
    # 推送 SSO token 到 grok2api 管理接口（chenyme/grok2api v2 协议）。
    # POST <endpoint>/admin/api/tokens/add  body {"pool": ..., "tokens": [...]}
    # 后端自带去重，重复的会进 skipped 计数；不需要先 GET 再合并。
    import json
    import urllib3
    import requests
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            conf = json.load(f)
    except Exception as e:
        print(f"[Warn] 读取 config.json 失败，跳过推送: {e}")
        return

    api_conf = conf.get("api", {})
    endpoint = str(api_conf.get("endpoint", "")).strip().rstrip("/")
    api_token = str(api_conf.get("token", "")).strip()
    pool = str(api_conf.get("pool", "basic")).strip() or "basic"

    if not endpoint or not api_token:
        return

    tokens_to_push = [t for t in new_tokens if t]
    if not tokens_to_push:
        return

    url = f"{endpoint}/admin/api/tokens/add"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            url,
            json={"pool": pool, "tokens": tokens_to_push},
            headers=headers,
            timeout=60,
            verify=False,
        )
        if resp.status_code == 200:
            data = resp.json() if resp.text else {}
            count = data.get("count", len(tokens_to_push))
            skipped = data.get("skipped", 0)
            print(f"[*] SSO token 已推送到号池（pool={pool}, 新增={count}, 跳过={skipped}): {url}")
        else:
            print(f"[Warn] 推送 API 返回异常: HTTP {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[Warn] 推送 API 失败: {e}")


def run_single_registration(output_path=DEFAULT_SSO_FILE, extract_numbers=False):
    # 单轮流程：打开注册页 -> 完成注册 -> 获取 sso -> 写 txt。
    open_signup_page()
    email, dev_token = fill_email_and_submit()
    fill_code_and_submit(email, dev_token)
    profile = fill_profile_and_submit()
    # 注册完成后等浏览器跑完 SSO 重定向链落到 grok.com 并登录——grok.com 域的
    # 会话 cookie（含 cf_clearance / sso / sso-rw）此时才会真正写下来。
    if not wait_for_grok_com_landing():
        print("[Warn] 未能落到 grok.com 登录态，sso 质量可能受影响")
    sso_value = wait_for_sso_cookie()
    append_sso_to_txt(sso_value, output_path)

    if extract_numbers:
        extract_visible_numbers()

    result = {
        "email": email,
        "sso": sso_value,
        **profile,
    }

    if run_logger:
        run_logger.info(
            "注册成功 | email=%s | password=%s | given=%s | family=%s",
            email,
            profile.get("password", ""),
            profile.get("given_name", ""),
            profile.get("family_name", ""),
        )

    print(f"[*] 本轮注册完成，邮箱: {email}")
    return result


def load_run_count() -> int:
    # 从 config.json 读取默认执行轮数，配置不存在时返回 10。
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        import json
        with open(config_path, "r", encoding="utf-8") as f:
            conf = json.load(f)
        v = conf.get("run", {}).get("count")
        if isinstance(v, int) and v >= 0:
            return v
    except Exception:
        pass
    return 10


def main():
    global run_logger
    run_logger = setup_run_logger()

    config_count = load_run_count()

    parser = argparse.ArgumentParser(description="Grok 自动注册机")
    parser.add_argument("--count", type=int, default=config_count, help=f"执行轮数，0 表示无限循环（默认 {config_count}）")
    parser.add_argument("--output", default=DEFAULT_SSO_FILE, help="sso 输出 txt 路径")
    parser.add_argument("--extract-numbers", action="store_true", help="注册完成后额外提取页面数字文本")
    args = parser.parse_args()

    total = args.count if args.count > 0 else '∞'
    print(f"")
    print(f"══════════════════════════════════════")
    print(f"  Grok 注册机启动")
    print(f"  计划轮数: {total}")
    print(f"  SSO 输出: {args.output}")
    print(f"══════════════════════════════════════")

    current_round = 0
    success_count = 0
    fail_count = 0
    collected_sso: list = []
    try:
        start_browser()
        while True:
            if args.count > 0 and current_round >= args.count:
                break

            current_round += 1
            print(f"")
            print(f"─── 第 {current_round}/{total} 轮 ────────────────────────")

            try:
                result = run_single_registration(args.output, extract_numbers=args.extract_numbers)
                collected_sso.append(result["sso"])
                success_count += 1
                print(f"✔ 第 {current_round} 轮成功 | {result['email']}")
            except KeyboardInterrupt:
                print(f"")
                print(f"[Info] 收到中断信号，停止后续轮次。")
                break
            except Exception as error:
                fail_count += 1
                print(f"✘ 第 {current_round} 轮失败 | {error}")
            finally:
                restart_browser()

            if args.count == 0 or current_round < args.count:
                time.sleep(0.5)

    finally:
        stop_browser()
        print(f"")
        print(f"══════════════════════════════════════")
        print(f"  注册机运行结束")
        print(f"  成功: {success_count}  失败: {fail_count}  共计: {current_round}")
        if collected_sso:
            print(f"  SSO 已保存到: {args.output}")
        print(f"══════════════════════════════════════")


if __name__ == "__main__":
    main()
