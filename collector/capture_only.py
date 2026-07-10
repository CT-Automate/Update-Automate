import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml
from playwright.sync_api import Playwright, sync_playwright

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

os.environ.pop("PWDEBUG", None)

SCREENSHOT_CLIP = {
    "x": 0,
    "y": 63,
    "width": 2100,
    "height": 840,
}
CLIP_TOP = 63


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


def truthy_env(name: str, default: str = "false") -> bool:
    return env_or_default(name, default).lower() in {"1", "true", "yes", "on"}


def disabled_env(name: str, default: str = "false") -> bool:
    return env_or_default(name, default).lower() in {"0", "false", "no", "off"}


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
        wait_for_visible_stable(
            page,
            [".react-flow__node"],
            timeout=timeout,
            stable_ms=850,
            label="nexis_monitor",
        )
    except Exception as exc:
        print(f"WARNING: monitor DOM did not settle before capture: {type(exc).__name__}")


def capture(page, filepath: Path) -> None:
    wait_for_monitor_ready(page)
    clip = compute_clip(page)
    page.screenshot(path=str(filepath), clip=clip)
    print(f"Captured temporary screenshot ({int(clip['width'])}x{int(clip['height'])}).")


def step(name: str, fn, failures: list[tuple[str, str]]) -> bool:
    started = time.perf_counter()
    status = "ok"
    for attempt in range(1, 4):
        try:
            fn()
            print(f"[OK] {name}")
            break
        except Exception as exc:
            print(f"[FAIL] {name} attempt {attempt}: {type(exc).__name__}")
            if attempt == 3:
                status = "failed"
                failures.append((name, type(exc).__name__))
                break
            time.sleep(1)

    log_timing("nexis_capture", f"shot:{name}", time.perf_counter() - started, status=status)
    return status == "ok"


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


def click_required(page, locator, label: str, timeout: int = 15000) -> None:
    try:
        locator.wait_for(state="visible", timeout=timeout)
        locator.click(timeout=timeout)
    except Exception as exc:
        raise RuntimeError(
            f"Could not click {label}. Current page: {safe_page_location(page)}; title: {safe_page_title(page)}"
        ) from exc


def run(playwright: Playwright) -> None:
    run_started = time.perf_counter()
    settings = load_settings()
    now = datetime.now()
    temp_root = Path(env_or_default("NEXIS_TEMP_ROOT", os.getenv("RUNNER_TEMP") or os.getenv("TEMP") or "/tmp"))
    temp_root.mkdir(parents=True, exist_ok=True)
    screenshot_path = temp_root / f"nexis_{now.strftime('%Y%m%d_%H%M%S')}_first.png"
    failures: list[tuple[str, str]] = []
    captured = False
    context = None
    page = None

    print("Starting ephemeral Nexus capture. The screenshot will be deleted after analysis.")

    try:
        context = playwright.chromium.launch_persistent_context(**browser_context_kwargs(settings))
        page = context.new_page()
        page.on("pageerror", lambda exc: print("PAGE ERROR: browser page error occurred"))
        page.set_viewport_size({"width": 1920, "height": 1080})

        response = page.goto(env_or_default("NEXIS_URL", "https://app.nexs.lenskart.com/"), timeout=45000)
        if response is not None:
            print(f"Initial page load status: {response.status}; page: {safe_page_location(page)}")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        try:
            employee_code = page.get_by_role("textbox", name="Employee Code")
            employee_code.wait_for(timeout=15000)
            print("Login page detected. Performing login.")
            employee_code.fill(require_env("NEXIS_EMPLOYEE_CODE"))
            page.get_by_role("textbox", name="Password").fill(require_env("NEXIS_PASSWORD"))
            page.get_by_role("button", name="LOGIN").click()
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
        except Exception:
            print(
                "Login fields were not visible. Checking for an existing authenticated session. "
                f"Current page: {safe_page_location(page)}; title: {safe_page_title(page)}"
            )

        page.evaluate("document.body.style.zoom='80%'")
        click_required(page, page.get_by_role("button", name="menu"), "menu button")
        click_required(page, page.get_by_role("menuitem", name="Monitor Panel"), "Monitor Panel menu item")

        facility = env_or_default("NEXIS_FACILITY", settings.get("facility", "NXS1"))
        click_required(page, page.locator("header").get_by_role("button", name="Open"), "facility selector")
        page.get_by_role("combobox", name="Select Facility").fill(facility.lower(), timeout=15000)
        click_required(page, page.get_by_role("option", name=facility, exact=True), f"facility option {facility}")
        wait_for_monitor_ready(page)

        captured = step("First", lambda: capture(page, screenshot_path), failures)

    except Exception as exc:
        print(f"ERROR: capture failed: {type(exc).__name__}: {exc}")
    finally:
        log_timing(
            "nexis_capture",
            "run_total",
            time.perf_counter() - run_started,
            status="ok" if captured else "no_screenshots",
            details={"captured": int(captured), "failures": len(failures)},
        )
        if context is not None:
            try:
                context.close()
            except Exception:
                pass

    if not screenshot_path.exists():
        print("No Nexus screenshot captured this run.", file=sys.stderr)
        sys.exit(1)

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
    if disabled_env("NEXIS_WRITE_JSON", "true"):
        analyze_cmd.append("--no-json")

    try:
        subprocess.run(analyze_cmd, check=True)
    finally:
        try:
            screenshot_path.unlink(missing_ok=True)
            print("Deleted temporary screenshot.")
        except Exception as exc:
            print(f"WARNING: could not delete temporary screenshot: {type(exc).__name__}")


if __name__ == "__main__":
    with sync_playwright() as playwright:
        run(playwright)
