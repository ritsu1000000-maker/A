from __future__ import annotations

import base64
import binascii
import ipaddress
import os
import re
import socket
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import unquote_to_bytes, urlparse

import requests
from flask import Flask, jsonify, render_template, request
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.utils import secure_filename
import scratchattach as sa

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024
app.config["MAX_FORM_MEMORY_SIZE"] = 80 * 1024 * 1024

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ALLOWED_MIME = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
}

MAX_INPUT_BYTES = 50 * 1024 * 1024
STATIC_MAX_W = 480
STATIC_MAX_H = 360

GIF_TARGET_BYTES = 900_000
GIF_MAX_W = 480
GIF_MAX_H = 360

MIN_ACTION_INTERVAL_SECONDS = 8

_lock = threading.Lock()
_last_action_at = 0.0


def _error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def _extract_project_id(value: str) -> str | None:
    value = (value or "").strip()

    if not value:
        return None

    if re.fullmatch(r"\d+", value):
        return value

    match = re.search(r"(?:scratch\.mit\.edu/)?projects/(\d+)", value, re.I)

    if match:
        return match.group(1)

    return None


def _parse_link_size(value: str, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}は整数で入力してください。")

    if not 1 <= number <= 9999:
        raise ValueError(f"{name}は1〜9999pxで入力してください。")

    return number


def _is_public_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)

        if parsed.scheme not in {"http", "https"}:
            return False

        if not parsed.hostname:
            return False

        if parsed.hostname.lower() == "localhost":
            return False

        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        for info in socket.getaddrinfo(parsed.hostname, port):
            ip = ipaddress.ip_address(info[4][0])

            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                return False

        return True

    except Exception:
        return False


def _suffix_for_mime(mime: str) -> str:
    mime = mime.lower()

    if mime in {"image/jpeg", "image/jpg"}:
        return ".jpg"

    if mime == "image/webp":
        return ".webp"

    if mime == "image/gif":
        return ".gif"

    return ".png"


def _write_temp_bytes(data: bytes, suffix: str) -> str:
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("入力画像は50MB以下にしてください。")

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)

    try:
        tmp.write(data)
        tmp.close()
        return tmp.name

    except Exception:
        tmp.close()

        try:
            os.remove(tmp.name)
        except OSError:
            pass

        raise


def _decode_data_url(value: str) -> str:
    match = re.match(
        r"^data:([^;,]+)((?:;[^,]*)*),(.*)$",
        value,
        re.I | re.S,
    )

    if not match:
        raise ValueError("Data URLの形式が正しくありません。")

    mime = match.group(1).strip().lower()
    params = match.group(2).lower()
    payload = match.group(3)

    if mime not in ALLOWED_MIME:
        raise ValueError("Data URLはPNG/JPEG/WEBP/GIFに対応しています。")

    try:
        if ";base64" in params:
            raw = base64.b64decode(
                re.sub(r"\s+", "", payload),
                validate=True,
            )
        else:
            raw = unquote_to_bytes(payload)

    except (binascii.Error, ValueError) as exc:
        raise ValueError("Data URLをデコードできません。") from exc

    return _write_temp_bytes(raw, _suffix_for_mime(mime))


def _download_image(url: str) -> str:
    if not _is_public_http_url(url):
        raise ValueError("公開されたhttp/https画像URLを入力してください。")

    with requests.get(
        url,
        stream=True,
        timeout=(8, 25),
        allow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 ScratchThumbnailTool/1.0"},
    ) as response:
        response.raise_for_status()

        if not _is_public_http_url(response.url):
            raise ValueError("リダイレクト先のURLを使用できません。")

        content_type = (
            (response.headers.get("content-type") or "")
            .split(";", 1)[0]
            .strip()
            .lower()
        )

        if content_type not in ALLOWED_MIME:
            raise ValueError("URLの内容が対応画像ではありません。")

        tmp = tempfile.NamedTemporaryFile(
            delete=False,
            suffix=_suffix_for_mime(content_type),
        )

        total = 0

        try:
            for chunk in response.iter_content(65536):
                if not chunk:
                    continue

                total += len(chunk)

                if total > MAX_INPUT_BYTES:
                    raise ValueError("URL画像は50MB以下にしてください。")

                tmp.write(chunk)

            tmp.close()
            return tmp.name

        except Exception:
            tmp.close()

            try:
                os.remove(tmp.name)
            except OSError:
                pass

            raise


def _source_from_text(value: str) -> tuple[str, str]:
    value = value.strip()

    if value.lower().startswith("data:image/"):
        return _decode_data_url(value), "data"

    return _download_image(value), "url"


def _read_animation_info(path: str) -> tuple[bool, int, str]:
    try:
        with Image.open(path) as image:
            frame_count = int(getattr(image, "n_frames", 1) or 1)
            animated = bool(getattr(image, "is_animated", False)) and frame_count > 1
            fmt = (image.format or "UNKNOWN").upper()

            return animated, frame_count, fmt

    except UnidentifiedImageError as exc:
        raise ValueError("入力データを画像として読み込めませんでした。") from exc


def _prepare_static_png(path: str) -> tuple[str, dict]:
    out = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    out_path = out.name
    out.close()

    try:
        with Image.open(path) as image:
            try:
                image.seek(0)
            except Exception:
                pass

            image = image.convert("RGBA")

            fitted = ImageOps.contain(
                image,
                (STATIC_MAX_W, STATIC_MAX_H),
                method=Image.Resampling.LANCZOS,
            )

            canvas = Image.new(
                "RGBA",
                (STATIC_MAX_W, STATIC_MAX_H),
                (255, 255, 255, 0),
            )

            x = (STATIC_MAX_W - fitted.width) // 2
            y = (STATIC_MAX_H - fitted.height) // 2

            canvas.alpha_composite(fitted, (x, y))

            canvas.save(
                out_path,
                format="PNG",
                optimize=True,
                compress_level=9,
            )

        return out_path, {
            "format": "PNG",
            "animated": False,
            "upload_width": STATIC_MAX_W,
            "upload_height": STATIC_MAX_H,
            "frames": 1,
            "bytes": os.path.getsize(out_path),
        }

    except Exception:
        try:
            os.remove(out_path)
        except OSError:
            pass

        raise


def _frame_indices(frame_count: int, step: int) -> list[int]:
    values = list(range(0, frame_count, max(1, step)))

    if values[-1] != frame_count - 1:
        values.append(frame_count - 1)

    return values


def _write_gif_attempt(
    source_path: str,
    scale_factor: float,
    colors: int,
    step: int,
) -> tuple[str, dict]:
    with Image.open(source_path) as source:
        frame_count = int(getattr(source, "n_frames", 1) or 1)
        original_w, original_h = source.size

        base_scale = min(
            1.0,
            GIF_MAX_W / max(1, original_w),
            GIF_MAX_H / max(1, original_h),
        )

        width = max(48, int(round(original_w * base_scale * scale_factor)))
        height = max(36, int(round(original_h * base_scale * scale_factor)))

        keep = _frame_indices(frame_count, step)

        original_durations = []

        for i in range(frame_count):
            source.seek(i)
            original_durations.append(
                max(20, int(source.info.get("duration", 100) or 100))
            )

        frames = []
        durations = []

        for position, frame_index in enumerate(keep):
            source.seek(frame_index)

            frame = source.convert("RGBA")

            fitted = ImageOps.contain(
                frame,
                (width, height),
                method=Image.Resampling.LANCZOS,
            )

            canvas = Image.new(
                "RGBA",
                (width, height),
                (0, 0, 0, 0),
            )

            x = (width - fitted.width) // 2
            y = (height - fitted.height) // 2

            canvas.alpha_composite(fitted, (x, y))

            paletted = canvas.convert(
                "P",
                palette=Image.Palette.ADAPTIVE,
                colors=colors,
                dither=Image.Dither.FLOYDSTEINBERG,
            )

            frames.append(paletted)

            next_index = (
                keep[position + 1]
                if position + 1 < len(keep)
                else frame_count
            )

            durations.append(
                max(
                    20,
                    sum(original_durations[frame_index:next_index]),
                )
            )

        out = tempfile.NamedTemporaryFile(delete=False, suffix=".gif")
        out_path = out.name
        out.close()

        frames[0].save(
            out_path,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=int(source.info.get("loop", 0) or 0),
            optimize=True,
            disposal=2,
        )

        return out_path, {
            "format": "GIF",
            "animated": True,
            "upload_width": width,
            "upload_height": height,
            "frames": len(frames),
            "bytes": os.path.getsize(out_path),
            "original_frames": frame_count,
        }


def _prepare_animated_gif(path: str) -> tuple[str, dict]:
    attempts = [
        (1.00, 128, 1),
        (1.00, 96, 1),
        (0.90, 96, 1),
        (0.82, 80, 1),
        (0.74, 64, 1),
        (0.66, 64, 1),
        (0.58, 48, 1),
        (0.52, 48, 2),
        (0.46, 40, 2),
        (0.40, 32, 3),
        (0.34, 32, 3),
        (0.30, 24, 4),
        (0.26, 24, 5),
    ]

    best_path = None
    best_info = None
    best_size = None

    for factor, colors, step in attempts:
        candidate_path, info = _write_gif_attempt(
            path,
            factor,
            colors,
            step,
        )

        size = info["bytes"]

        if best_size is None or size < best_size:
            if best_path:
                try:
                    os.remove(best_path)
                except OSError:
                    pass

            best_path = candidate_path
            best_info = info
            best_size = size

        else:
            try:
                os.remove(candidate_path)
            except OSError:
                pass

        if size <= GIF_TARGET_BYTES:
            return best_path, best_info

    if best_path:
        return best_path, best_info

    raise ValueError("GIFの圧縮に失敗しました。")


def _prepare_thumbnail(path: str) -> tuple[str, dict]:
    animated, frame_count, fmt = _read_animation_info(path)

    if animated:
        prepared, info = _prepare_animated_gif(path)
        info["original_format"] = fmt
        info["original_frames"] = frame_count
        return prepared, info

    prepared, info = _prepare_static_png(path)
    info["original_format"] = fmt
    info["original_frames"] = frame_count
    return prepared, info


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.post("/api/upload")
def upload():
    global _last_action_at

    mode = (request.form.get("mode") or "").strip()
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    image = request.files.get("image")
    image_source = (request.form.get("image_source") or "").strip()

    if mode not in {"create", "overwrite"}:
        return _error("モードが不正です。")

    if not username:
        return _error("Scratchユーザー名を入力してください。")

    if not password:
        return _error("Scratchパスワードを入力してください。")

    try:
        link_width = _parse_link_size(
            request.form.get("link_width"),
            "リンクの幅",
        )

        link_height = _parse_link_size(
            request.form.get("link_height"),
            "リンクの高さ",
        )

    except ValueError as exc:
        return _error(str(exc))

    has_file = image is not None and bool(image.filename)
    has_source = bool(image_source)

    if not has_file and not has_source:
        return _error("画像ファイルまたは画像URL/Data URLを入力してください。")

    if has_file and has_source:
        return _error("ファイルとURL/Data URLはどちらか一方にしてください。")

    with _lock:
        now = time.monotonic()
        wait = MIN_ACTION_INTERVAL_SECONDS - (now - _last_action_at)

        if wait > 0:
            return _error(
                f"連続操作防止中です。あと約{int(wait) + 1}秒待ってください。",
                429,
            )

        _last_action_at = now

    original_path = None
    prepared_path = None

    try:
        source_kind = "file"

        if has_file:
            suffix = Path(
                secure_filename(image.filename)
            ).suffix.lower()

            if suffix not in ALLOWED_EXTENSIONS:
                return _error("PNG/JPEG/WEBP/GIFに対応しています。")

            tmp = tempfile.NamedTemporaryFile(
                delete=False,
                suffix=suffix or ".img",
            )

            image.save(tmp)
            original_path = tmp.name
            tmp.close()

        else:
            original_path, source_kind = _source_from_text(
                image_source
            )

        prepared_path, media = _prepare_thumbnail(
            original_path
        )

        session = sa.login(
            username,
            password,
        )

        if mode == "create":
            title = (
                request.form.get("title") or "Image upload"
            ).strip()[:100]

            created = session.create_project(
                title=title or "Image upload"
            )

            project_id = str(created.id)

        else:
            project_id = _extract_project_id(
                (request.form.get("project_url") or "").strip()
            )

            if not project_id:
                return _error(
                    "有効なScratchプロジェクトURLまたはIDを入力してください。"
                )

        project = session.connect_project(
            project_id
        )

        project.set_thumbnail(
            file=prepared_path
        )

        encoded_url = (
            "https://scratch.mit.edu/"
            "get_i%6d%61ge/p%72oject/"
            f"{project_id}_{link_width}x{link_height}.png"
        )

        normal_url = (
            "https://scratch.mit.edu/"
            "get_image/project/"
            f"{project_id}_{link_width}x{link_height}.png"
        )

        project_url = (
            f"https://scratch.mit.edu/projects/{project_id}/"
        )

        return jsonify({
            "ok": True,
            "mode": mode,
            "project_id": int(project_id),
            "project_url": project_url,
            "encoded_url": encoded_url,
            "normal_url": normal_url,
            "source": source_kind,
            "upload_format": media["format"],
            "upload_animated": media["animated"],
            "upload_width": media["upload_width"],
            "upload_height": media["upload_height"],
            "upload_frames": media["frames"],
            "original_frames": media["original_frames"],
            "upload_bytes": media["bytes"],
        })

    except ValueError as exc:
        return _error(str(exc), 400)

    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__

        if len(message) > 600:
            message = message[:600] + "..."

        return _error(
            f"処理に失敗しました: {message}",
            500,
        )

    finally:
        for path in (original_path, prepared_path):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass


@app.errorhandler(413)
def too_large(_):
    return _error("入力データが大きすぎます。", 413)


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=8765,
        debug=False,
    )
