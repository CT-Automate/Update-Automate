from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps


DEFAULT_FONT_PATHS = [
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\ARIALN.TTF",
    r"C:\Windows\Fonts\calibri.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\segoeuil.ttf",
]

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

DEFAULT_STATUS_SYMBOLS = {
    "green": "\U0001F7E2",
    "amber": "\U0001F7E0",
    "red": "\U0001F534",
    "unknown": "\u26AA",
}

@dataclass(frozen=True)
class NodeConfig:
    name: str
    label: str
    value_box: tuple[int, int, int, int]
    green_max: int
    amber_max: int


@dataclass(frozen=True)
class DigitRead:
    value: int | None
    raw_text: str
    confidence: float
    digit_scores: list[float]


class DigitRecognizer:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        configured_fonts = config.get("font_paths") or []
        self.font_paths = self._existing_fonts([*configured_fonts, *DEFAULT_FONT_PATHS])
        self.dark_pixel_threshold = int(config.get("dark_pixel_threshold", 150))
        self.max_digit_score = float(config.get("max_digit_score", 0.62))
        self.min_glyph_height = int(config.get("min_glyph_height", 8))
        self.font_sizes = list(range(int(config.get("min_font_size", 16)), int(config.get("max_font_size", 34))))
        self._template_cache: dict[tuple[str, str, int, tuple[int, int]], np.ndarray] = {}

        if not self.font_paths:
            raise RuntimeError("No usable TrueType fonts found for digit recognition.")

    @staticmethod
    def _existing_fonts(font_paths: list[str]) -> list[Path]:
        seen: set[str] = set()
        fonts: list[Path] = []
        for font_path in font_paths:
            path = Path(font_path)
            key = str(path).lower()
            if key in seen or not path.exists():
                continue
            seen.add(key)
            fonts.append(path)
        return fonts

    def read_number(self, image: Image.Image) -> DigitRead:
        glyphs = self._glyph_masks(image)
        if not glyphs:
            return DigitRead(None, "", 0.0, [])

        digits: list[str] = []
        scores: list[float] = []
        for glyph in glyphs:
            digit, score = self._match_digit(glyph)
            if digit is None or score > self.max_digit_score:
                return DigitRead(None, "".join(digits), 0.0, scores)
            digits.append(digit)
            scores.append(round(float(score), 4))

        raw_text = "".join(digits)
        confidence = round(max(0.0, 1.0 - max(scores, default=1.0)), 3)
        return DigitRead(int(raw_text), raw_text, confidence, scores)

    def _glyph_masks(self, image: Image.Image) -> list[np.ndarray]:
        gray = np.array(ImageOps.grayscale(image))
        mask = gray < self.dark_pixel_threshold
        rows = np.where(mask.any(axis=1))[0]
        if rows.size == 0:
            return []

        col_has_pixels = mask.any(axis=0)
        runs: list[tuple[int, int]] = []
        start: int | None = None
        for index, has_pixels in enumerate(col_has_pixels):
            if has_pixels and start is None:
                start = index
            at_end = index == len(col_has_pixels) - 1
            if (not has_pixels or at_end) and start is not None:
                end = index - 1 if not has_pixels else index
                if end - start + 1 >= 2:
                    runs.append((start, end))
                start = None

        glyphs: list[np.ndarray] = []
        for x1, x2 in runs:
            run_mask = mask[:, x1 : x2 + 1]
            run_rows = np.where(run_mask.any(axis=1))[0]
            if run_rows.size == 0:
                continue
            height = int(run_rows[-1] - run_rows[0] + 1)
            if height < self.min_glyph_height:
                continue
            glyphs.append(run_mask[run_rows[0] : run_rows[-1] + 1, :])
        return glyphs

    def _match_digit(self, glyph: np.ndarray) -> tuple[str | None, float]:
        best_digit: str | None = None
        best_score = 1.0
        for font_path in self.font_paths:
            for font_size in self.font_sizes:
                for digit in "0123456789":
                    template = self._render_digit(digit, font_path, font_size, glyph.shape)
                    score = self._mask_distance(glyph, template)
                    if score < best_score:
                        best_score = score
                        best_digit = digit
        return best_digit, best_score

    def _render_digit(self, digit: str, font_path: Path, font_size: int, target_shape: tuple[int, int]) -> np.ndarray:
        key = (digit, str(font_path), font_size, target_shape)
        if key in self._template_cache:
            return self._template_cache[key]

        font = ImageFont.truetype(str(font_path), font_size)
        canvas = Image.new("L", (96, 96), 255)
        draw = ImageDraw.Draw(canvas)
        draw.text((20, 20), digit, font=font, fill=0)
        template = np.array(canvas) < self.dark_pixel_threshold
        rows, cols = np.where(template)
        if rows.size == 0:
            rendered = np.zeros(target_shape, dtype=bool)
        else:
            template = template[rows.min() : rows.max() + 1, cols.min() : cols.max() + 1]
            target_height, target_width = target_shape
            rendered_image = Image.fromarray((~template).astype("uint8") * 255).resize(
                (target_width, target_height),
                Image.Resampling.LANCZOS,
            )
            rendered = np.array(rendered_image) < self.dark_pixel_threshold

        self._template_cache[key] = rendered
        return rendered

    @staticmethod
    def _mask_distance(left: np.ndarray, right: np.ndarray) -> float:
        intersection = np.logical_and(left, right).sum()
        union = np.logical_or(left, right).sum()
        if union == 0:
            return 1.0
        return float(1.0 - (intersection / union))


def safe_print(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        print(message.encode(encoding, errors="backslashreplace").decode(encoding))

def load_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def resolve_workspace_path(path_value: str | Path, *, base: Path = PROJECT_ROOT) -> Path:
    path = Path(path_value).expanduser()
    if path.is_absolute():
        return path
    return base / path


def resolve_cli_path(path_value: str | Path) -> Path:
    path = Path(path_value).expanduser()
    if path.is_absolute():
        return path

    cwd_path = Path.cwd() / path
    if cwd_path.exists():
        return cwd_path
    return PROJECT_ROOT / path


def classify_value(value: int | None, green_max: int, amber_max: int) -> str:
    if value is None:
        return "unknown"
    if value <= green_max:
        return "green"
    if value <= amber_max:
        return "amber"
    return "red"


def get_status_symbols(config: dict[str, Any]) -> dict[str, str]:
    configured = config.get("slack", {}).get("status_symbols", {})
    return {**DEFAULT_STATUS_SYMBOLS, **configured}


def read_node_limit(raw_node: dict[str, Any], key: str) -> int:
    if key not in raw_node:
        raise ValueError(f"Node {raw_node.get('name', '<unknown>')} is missing {key}.")
    return int(raw_node[key])


def scale_box(
    box: tuple[int, int, int, int],
    width: int,
    height: int,
    ref_width: int,
    ref_height: int,
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    scale_x = width / ref_width
    scale_y = height / ref_height
    return (
        int(round(x1 * scale_x)),
        int(round(y1 * scale_y)),
        int(round(x2 * scale_x)),
        int(round(y2 * scale_y)),
    )


def extract_nodes(image_path: Path, config: dict[str, Any], recognizer: DigitRecognizer) -> list[dict[str, Any]]:
    layout = config["layout"]
    ref_width = int(layout["reference_width"])
    ref_height = int(layout["reference_height"])
    symbols = get_status_symbols(config)

    with Image.open(image_path) as source:
        image = source.convert("RGB")
        nodes: list[dict[str, Any]] = []
        for raw_node in layout["nodes"]:
            node = NodeConfig(
                name=raw_node["name"],
                label=raw_node["label"],
                value_box=tuple(raw_node["value_box"]),
                green_max=read_node_limit(raw_node, "green_max"),
                amber_max=read_node_limit(raw_node, "amber_max"),
            )
            box = scale_box(node.value_box, image.width, image.height, ref_width, ref_height)
            reading = recognizer.read_number(image.crop(box))
            status = classify_value(reading.value, node.green_max, node.amber_max)
            nodes.append(
                {
                    "name": node.name,
                    "label": node.label,
                    "value": reading.value,
                    "raw_text": reading.raw_text,
                    "confidence": reading.confidence,
                    "green_max": node.green_max,
                    "amber_max": node.amber_max,
                    "red_min": node.amber_max + 1,
                    "status": status,
                    "symbol": symbols[status],
                    "breached": status == "red",
                    "digit_scores": reading.digit_scores,
                    "crop_box": list(box),
                }
            )
    return nodes


def is_image_candidate(path: Path, image_extensions: list[str]) -> bool:
    if not path.is_file():
        return False
    if path.name.startswith("."):
        return False
    if not path.suffix:
        return True
    return path.suffix.lower() in {extension.lower() for extension in image_extensions}


def discover_images(data_root: Path, image_extensions: list[str]) -> list[dict[str, Any]]:
    discovered: list[dict[str, Any]] = []
    if not data_root.exists():
        return discovered

    for date_dir in sorted(path for path in data_root.iterdir() if path.is_dir()):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_dir.name):
            continue

        for time_path in sorted(date_dir.iterdir()):
            if time_path.is_file():
                time_key = time_path.stem if time_path.suffix else time_path.name
                if re.fullmatch(r"\d{4}", time_key) and is_image_candidate(time_path, image_extensions):
                    discovered.append({"date": date_dir.name, "time_key": time_key, "image_path": time_path})
                continue

            if time_path.is_dir() and re.fullmatch(r"\d{4}", time_path.name):
                images = sorted(path for path in time_path.iterdir() if is_image_candidate(path, image_extensions))
                if images:
                    discovered.append({"date": date_dir.name, "time_key": time_path.name, "image_path": images[0]})

    return discovered


def infer_date_time(image_path: Path) -> tuple[str, str]:
    parent = image_path.parent
    if re.fullmatch(r"\d{4}", parent.name) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parent.parent.name):
        return parent.parent.name, parent.name
    if re.fullmatch(r"\d{4}", image_path.stem) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parent.name):
        return parent.name, image_path.stem

    modified_at = datetime.fromtimestamp(image_path.stat().st_mtime)
    return modified_at.strftime("%Y-%m-%d"), modified_at.strftime("%H%M")


def build_payload(entry: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    observed_at = datetime.strptime(f"{entry['date']} {entry['time_key']}", "%Y-%m-%d %H%M")
    breached_nodes = [node["name"] for node in nodes if node["breached"]]
    read_failed_nodes = [node["name"] for node in nodes if node["value"] is None]
    if read_failed_nodes:
        overall_status = "READ_ERROR"
    elif breached_nodes:
        overall_status = "BREACH"
    else:
        overall_status = "OK"

    return {
        "date": entry["date"],
        "time": entry["time_key"],
        "year": observed_at.year,
        "hour": observed_at.strftime("%H"),
        "image_path": str(entry["image_path"]),
        "observed_at": observed_at.isoformat(timespec="minutes"),
        "processed_at": datetime.now().isoformat(timespec="seconds"),
        "status": overall_status,
        "nodes": nodes,
        "has_breach": bool(breached_nodes),
        "breached_nodes": breached_nodes,
        "has_read_error": bool(read_failed_nodes),
        "read_failed_nodes": read_failed_nodes,
    }


def write_json(output_root: Path, payload: dict[str, Any]) -> Path:
    target_dir = output_root / str(payload["year"]) / payload["date"]
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{payload['time']}.json"
    with target_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return target_path


def should_write_json() -> bool:
    value = os.getenv("NEXIS_WRITE_JSON", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def format_node_caption(node: dict[str, Any]) -> str:
    value = node["value"] if node["value"] is not None else "NA"
    return f"{node['label']} = {value} {node['symbol']}"


def format_slack_message(payload: dict[str, Any], *, breach_only: bool = False) -> str:
    if breach_only:
        nodes = [node for node in payload["nodes"] if node["status"] in {"amber", "red"}]
        header = f"[BREACH] {payload['date']} {payload['time']}"
    elif payload.get("has_read_error"):
        nodes = payload["nodes"]
        header = f"[READ_ERROR] {payload['date']} {payload['time']}"
    else:
        nodes = payload["nodes"]
        header = f"{payload['date']} {payload['time']}"

    lines = [format_node_caption(node) for node in nodes]
    return "\n".join([header, *lines])


def post_to_slack(webhook_url: str, message: str, username: str) -> None:
    import requests

    response = requests.post(webhook_url, json={"text": message, "username": username}, timeout=15)
    response.raise_for_status()


def post_to_slack_channel(bot_token: str, channel: str, message: str, username: str) -> None:
    import requests

    response = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {bot_token}"},
        json={
            "channel": channel,
            "text": message,
            "username": username,
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("error", "Slack API request failed"))


def has_attention_nodes(payload: dict[str, Any]) -> bool:
    return any(node["status"] in {"amber", "red"} for node in payload["nodes"])


def breach_updates_interval_hours() -> int:
    raw_value = os.getenv("SLACK_BREACH_UPDATES_INTERVAL_HOURS", "10").strip()
    try:
        interval = int(raw_value)
    except ValueError:
        interval = 10
    return max(1, interval)


def should_send_breach_update(payload: dict[str, Any]) -> bool:
    if not has_attention_nodes(payload):
        return False

    observed_at = datetime.fromisoformat(payload["observed_at"])
    interval = breach_updates_interval_hours()
    return observed_at.hour % interval == 0


def maybe_send_slack(config: dict[str, Any], payload: dict[str, Any], enabled: bool) -> None:
    slack = config.get("slack", {})
    env_enabled = os.getenv("SLACK_ENABLED")
    slack_enabled = slack.get("enabled")
    if env_enabled is not None:
        slack_enabled = env_enabled.strip().lower() in {"1", "true", "yes", "on"}
    if not enabled or not slack_enabled:
        return

    username = slack.get("username", "Nexis Monitor")
    all_updates = os.getenv("SLACK_ALL_UPDATES_WEBHOOK", slack.get("all_updates_webhook", "")).strip()
    breach_updates = os.getenv("SLACK_BREACH_UPDATES_WEBHOOK", slack.get("breach_updates_webhook", "")).strip()
    bot_token = os.getenv("SLACK_BOT_TOKEN", slack.get("bot_token", "")).strip()
    all_updates_channel = os.getenv("SLACK_ALL_UPDATES_CHANNEL", slack.get("all_updates_channel", "")).strip()
    breach_updates_channel = os.getenv("SLACK_BREACH_UPDATES_CHANNEL", slack.get("breach_updates_channel", "")).strip()

    if bot_token and all_updates_channel:
        post_to_slack_channel(bot_token, all_updates_channel, format_slack_message(payload), username)
    elif all_updates:
        post_to_slack(all_updates, format_slack_message(payload), username)

    if should_send_breach_update(payload):
        if bot_token and breach_updates_channel:
            post_to_slack_channel(bot_token, breach_updates_channel, format_slack_message(payload, breach_only=True), username)
        elif breach_updates:
            post_to_slack(breach_updates, format_slack_message(payload, breach_only=True), username)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze Nexus panel screenshots and emit JSON plus Slack updates.")
    parser.add_argument(
        "--config",
        default="collector/config.local.json",
        help="Path to config JSON. Falls back to collector/config.example.json if the local file does not exist.",
    )
    parser.add_argument("--image", help="Process one screenshot directly instead of scanning data_root.")
    parser.add_argument("--date", help="Date key for --image, formatted as yyyy-mm-dd. Inferred when omitted.")
    parser.add_argument("--time", help="Time key for --image, formatted as hhmm. Inferred when omitted.")
    parser.add_argument("--latest", action="store_true", help="Only process the latest discovered screenshot.")
    parser.add_argument("--no-slack", action="store_true", help="Do not send Slack notifications.")
    parser.add_argument("--no-json", action="store_true", help="Do not persist extracted values to JSON.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path = resolve_cli_path(args.config)
    if not config_path.exists():
        config_path = PROJECT_ROOT / "collector" / "config.example.json"

    config = load_config(config_path)
    recognizer = DigitRecognizer(config.get("digit_recognition"))

    if args.image:
        image_path = Path(args.image)
        date_key, time_key = infer_date_time(image_path)
        entries = [
            {
                "date": args.date or date_key,
                "time_key": args.time or time_key,
                "image_path": image_path,
            }
        ]
    else:
        data_root = resolve_workspace_path(config["data_root"])
        entries = discover_images(data_root, config["image_extensions"])
        if args.latest and entries:
            entries = [entries[-1]]

    if not entries:
        safe_print("No screenshots found.")
        return

    output_root = resolve_workspace_path(config["output_root"])
    for entry in entries:
        nodes = extract_nodes(Path(entry["image_path"]), config, recognizer)
        payload = build_payload(entry, nodes)
        maybe_send_slack(config, payload, enabled=not args.no_slack)
        if should_write_json() and not args.no_json:
            output_path = write_json(output_root, payload)
            safe_print(f"Analysis complete. JSON written to {output_path}.")
        else:
            safe_print("Analysis complete. JSON persistence disabled.")


if __name__ == "__main__":
    main()


