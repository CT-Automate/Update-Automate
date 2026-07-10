from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml
from playwright.sync_api import Playwright, TimeoutError as PlaywrightTimeoutError, sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from monitoring.timing_log import log_timing
    from monitoring.dom_waits import wait_for_visible_stable
except Exception:
    def log_timing(*args: Any, **kwargs: Any) -> None:
        return

    wait_for_visible_stable = None

SCREENSHOT_CLIP = {"x": 0, "y": 63, "width": 2100, "height": 840}
CLIP_TOP = 63
os.environ.pop("PWDEBUG", None)


def env_or_default(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        return ""
    return str(value).strip()


def require_env(name: str, default: str | None = None) -> str:
    value = env_or_default(name, default)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

def load_env_file() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if (value.startswith("\"") and value.endswith("\"")) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ[key] = value



def disabled_env(name: str, default: str = "false") -> bool:
    return env_or_default(name, default).lower() in {"0", "false", "no", "off"}

def truthy_env(name: str, default: str = "false") -> bool:
    return env_or_default(name, default).lower() in {"1", "true", "yes", "on"}



def load_settings() -> dict[str, Any]:
    settings_path = PROJECT_ROOT / "settings.yaml"
    if not settings_path.exists():
        return {}
    with settings_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def browser_context_kwargs(settings: dict[str, Any]) -> dict[str, Any]:
    user_data_dir = env_or_default("NEXIS_USER_DATA_DIR", settings.get("user_data_dir", ""))
    if not user_data_dir:
        user_data_dir = str(Path(os.getenv("RUNNER_TEMP") or os.getenv("TEMP") or "/tmp") / "nexis-browser-profile")

    kwargs: dict[str, Any] = {
        "user_data_dir": user_data_dir,
        "headless": True,
        "viewport": None,
        "args": ["--start-maximized", "--force-device-scale-factor=0.8"],
    }

    browser_channel = env_or_default("NEXIS_BROWSER_CHANNEL", settings.get("browser_channel", ""))
    if browser_channel and browser_channel.lower() != "chromium":
        kwargs["channel"] = browser_channel
    return kwargs


def safe_page_location(page) -> str:
    try:
        parts = urlsplit(page.url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return "<unknown>"


def safe_page_title(page) -> str:
    try:
        return page.title()[:120]
    except Exception:
        return "<unknown>"


def dump_page_snapshot(page, label: str) -> None:
    try:
        print(f"[{label}] url: {safe_page_location(page)}")
        print(f"[{label}] title: {safe_page_title(page)}")
        buttons = page.locator("button")
        button_count = min(buttons.count(), 20)
        if button_count:
            print(f"[{label}] buttons:")
            for index in range(button_count):
                try:
                    text = buttons.nth(index).inner_text(timeout=2000).strip()
                except Exception:
                    text = "<unreadable>"
                print(f"  button[{index}]: {text[:120]}")
        links = page.locator("a")
        link_count = min(links.count(), 20)
        if link_count:
            print(f"[{label}] links:")
            for index in range(link_count):
                try:
                    text = links.nth(index).inner_text(timeout=2000).strip()
                except Exception:
                    text = "<unreadable>"
                print(f"  link[{index}]: {text[:120]}")
    except Exception as exc:
        print(f"[{label}] snapshot failed: {type(exc).__name__}: {exc}")


def assert_not_cloudflare_block(page, response_status: int | None) -> None:
    title = safe_page_title(page)
    if response_status == 403 and "Cloudflare" in title:
        raise RuntimeError("Nexus is blocked by Cloudflare on this runner.")
    if "Attention Required" in title and "Cloudflare" in title:
        raise RuntimeError("Nexus returned a Cloudflare challenge page instead of the app.")


def click_required(page, locator, label: str, timeout: int = 15000) -> None:
    try:
        locator.wait_for(state="visible", timeout=timeout)
        locator.click(timeout=timeout)
    except Exception as exc:
        dump_page_snapshot(page, f"click_failed:{label}")
        raise RuntimeError(
            f"Could not click {label}. Current page: {safe_page_location(page)}; title: {safe_page_title(page)}; underlying: {type(exc).__name__}: {exc}"
        ) from exc


def compute_clip(page, top: int = CLIP_TOP, padding: int = 14, min_width: int = 1100) -> dict[str, float]:
    box = page.evaluate(
        """() => {
            const rects = [...document.querySelectorAll('.react-flow__node')]
                .map(n => n.getBoundingClientRect())
                .filter(r => r.width > 0 && r.height > 0);
            if (!rects.length) return null;
            return {
                right: Math.max(...rects.map(r => r.right)),
                bottom: Math.max(...rects.map(r => r.bottom)),
            };
        }"""
    )
    if not box:
        return dict(SCREENSHOT_CLIP)

    viewport = page.viewport_size or {"width": 1920, "height": 1080}
    width = min(viewport["width"], max(box["right"], min_width) + padding)
    height = min(viewport["height"] - top, box["bottom"] - top + padding)
    if width <= 0 or height <= 0:
        return dict(SCREENSHOT_CLIP)
    return {"x": 0, "y": top, "width": float(width), "height": float(height)}


def wait_for_monitor_ready(page, timeout: int = 20000) -> None:
    try:
        if wait_for_visible_stable is None:
            page.locator(".react-flow__node").first.wait_for(state="visible", timeout=timeout)
            return
        wait_for_visible_stable(page, [".react-flow__node"], timeout=timeout, stable_ms=850, label="nexis_monitor")
    except Exception as exc:
        print(f"WARNING: monitor DOM did not settle before capture: {type(exc).__name__}")


def capture(page, filepath: Path) -> None:
    wait_for_monitor_ready(page)
    clip = compute_clip(page)
    page.screenshot(path=str(filepath), clip=clip)
    print(f"Captured temporary screenshot ({int(clip['width'])}x{int(clip['height'])}).")


def open_monitor_panel(page) -> None:
    click_required(page, page.get_by_role("button", name="menu"), "menu button")
    click_required(page, page.get_by_role("menuitem", name="Monitor Panel"), "Monitor Panel menu item")


def select_facility(page, facility: str) -> None:
    click_required(page, page.locator("div").filter(has_text=re.compile(r"^Select Facility$")), "Select Facility dropdown")
    page.get_by_role("combobox", name="Select Facility").fill(facility, timeout=15000)
    click_required(page, page.get_by_role("option", name=facility, exact=True), f"facility option {facility}")


def run(playwright: Playwright) -> None:
    run_started = time.perf_counter()
    settings = load_settings()
    load_env_file()
    now = datetime.now()
    screenshot_dir = Path(env_or_default("NEXIS_SCREENSHOT_DIR", str(PROJECT_ROOT / "data" / "latest")))
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = screenshot_dir / "first.png"
    context = None
    page = None

    print(f"Starting Nexus capture. Screenshot will be saved permanently at: {screenshot_path}")

    try:
        context = playwright.chromium.launch_persistent_context(**browser_context_kwargs(settings))
        page = context.new_page()
        page.set_viewport_size({"width": 1920, "height": 1080})

        response_status = None
        start_url = env_or_default("NEXIS_URL", "https://app.nexs.lenskart.com/")
        try:
            response = page.goto(start_url, wait_until="domcontentloaded", timeout=45000)
            if response is not None:
                response_status = response.status
                print(f"Initial page load status: {response.status}; page: {safe_page_location(page)}")
        except PlaywrightTimeoutError:
            print(f"Initial page navigation timed out. Page: {safe_page_location(page)}; title: {safe_page_title(page)}")
        assert_not_cloudflare_block(page, response_status)

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        current_path = safe_page_location(page)
        employee_code = page.get_by_role("textbox", name="Employee Code")
        login_visible = employee_code.count() > 0 and employee_code.first.is_visible()
        on_dashboard = current_path.endswith("/dashboard")

        if login_visible:
            print("Login page detected. Performing login.")
            employee_value = require_env("NEXIS_EMPLOYEE_CODE")
            password_value = require_env("NEXIS_PASSWORD")
            print(f"Filling login fields: employee_code_len={len(employee_value)}, password_len={len(password_value)}")
            employee_code.fill(employee_value)
            page.get_by_role("textbox", name="Password").fill(password_value)
            print("Login fields filled. Clicking LOGIN.")
            page.get_by_role("button", name="LOGIN").click()
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
        elif on_dashboard:
            print(f"Already authenticated on dashboard: {current_path}; skipping login.")
        else:
            dump_page_snapshot(page, "login_not_visible")
            raise RuntimeError(f"Login form is not visible. Current page: {safe_page_location(page)}; title: {safe_page_title(page)}")

        page.evaluate("document.body.style.zoom='80%'")
        open_monitor_panel(page)
        facility = env_or_default("NEXIS_FACILITY", settings.get("facility", "NXS1"))
        select_facility(page, facility)
        wait_for_monitor_ready(page)
        capture(page, screenshot_path)

    except Exception as exc:
        dump_page_snapshot(page, "run_failed")
        print(f"ERROR: capture failed: {type(exc).__name__}: {exc}")
        raise
    finally:
        log_timing(
            "nexis_capture",
            "run_total",
            time.perf_counter() - run_started,
            status="ok" if screenshot_path.exists() else "no_screenshots",
            details={"captured": int(screenshot_path.exists())},
        )
        if context is not None:
            try:
                context.close()
            except Exception:
                pass

    analyze_cmd = [
        sys.executable,
        str(PROJECT_ROOT / "collector" / "analyze_panel.py"),
        "--image",
        str(screenshot_path),
        "--date",
        now.strftime("%Y-%m-%d"),
        "--time",
        now.strftime("%H%M"),
    ]
    if disabled_env("SLACK_ENABLED", "true"):
        analyze_cmd.append("--no-slack")

    try:
        subprocess.run(analyze_cmd, check=True)
    finally:
        print(f"Screenshot kept permanently: {screenshot_path}")

if __name__ == "__main__":
    with sync_playwright() as playwright:
        run(playwright)



