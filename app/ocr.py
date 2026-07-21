from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import re
import shutil
import subprocess
import threading
from typing import Any, Callable, Protocol

from PIL import Image

from app.config import Settings


WHITESPACE_RE = re.compile(r"\s+")


class OcrProvider(Protocol):
    name: str

    def recognize(
        self,
        image: Image.Image,
        *,
        filename: str,
        page_number: int,
    ) -> str: ...


class TesseractOcrProvider:
    name = "tesseract"

    def __init__(
        self,
        *,
        command: str = "tesseract",
        language: str = "eng",
        timeout_seconds: int = 60,
    ) -> None:
        self._configured_command = command.strip() or "tesseract"
        self._language = language.strip() or "eng"
        self._timeout_seconds = max(1, timeout_seconds)

    def recognize(
        self,
        image: Image.Image,
        *,
        filename: str,
        page_number: int,
    ) -> str:
        command = resolve_tesseract_command(self._configured_command)
        image_buffer = io.BytesIO()
        image.save(image_buffer, format="PNG")
        raw_dpi = image.info.get("dpi", (300, 300))
        dpi = raw_dpi[0] if isinstance(raw_dpi, (list, tuple)) else raw_dpi
        try:
            normalized_dpi = max(1, int(float(dpi)))
        except (TypeError, ValueError):
            normalized_dpi = 300

        try:
            result = subprocess.run(
                [
                    command,
                    "stdin",
                    "stdout",
                    "-l",
                    self._language,
                    "--dpi",
                    str(normalized_dpi),
                ],
                input=image_buffer.getvalue(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"OCR timed out on page {page_number} of `{filename}` after "
                f"{self._timeout_seconds} seconds."
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"Could not start Tesseract OCR while processing `{filename}`."
            ) from exc

        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            detail = WHITESPACE_RE.sub(" ", detail)[:500]
            suffix = f" Tesseract reported: {detail}" if detail else ""
            raise RuntimeError(
                f"OCR failed on page {page_number} of `{filename}`.{suffix}"
            )

        return result.stdout.decode("utf-8", errors="replace").strip()


class RapidOcrProvider:
    name = "rapidocr"

    def __init__(
        self,
        *,
        engine_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._engine_factory = engine_factory
        self._engine: Any | None = None
        self._engine_lock = threading.Lock()

    def recognize(
        self,
        image: Image.Image,
        *,
        filename: str,
        page_number: int,
    ) -> str:
        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError(
                "RapidOCR requires NumPy. Install `requirements-rapidocr.txt` and retry."
            ) from exc

        engine = self._get_engine()
        try:
            result = engine(np.asarray(image.convert("RGB")))
        except Exception as exc:
            raise RuntimeError(
                f"RapidOCR failed on page {page_number} of `{filename}`."
            ) from exc

        texts = getattr(result, "txts", None) if result is not None else None
        if not texts:
            return ""
        return "\n".join(str(text).strip() for text in texts if str(text).strip())

    def _get_engine(self) -> Any:
        if self._engine is not None:
            return self._engine

        with self._engine_lock:
            if self._engine is not None:
                return self._engine
            try:
                if self._engine_factory is not None:
                    self._engine = self._engine_factory()
                else:
                    from rapidocr import RapidOCR

                    self._engine = RapidOCR()
            except ImportError as exc:
                raise RuntimeError(
                    "RapidOCR is not installed. Install `requirements-rapidocr.txt` and retry."
                ) from exc
            except Exception as exc:
                raise RuntimeError(
                    "RapidOCR could not initialize its local OCR models."
                ) from exc

        return self._engine


def create_ocr_provider(settings: Settings) -> OcrProvider:
    engine = normalize_ocr_engine(getattr(settings, "pdf_ocr_engine", "tesseract"))
    if engine == "tesseract":
        return TesseractOcrProvider(
            command=getattr(settings, "pdf_ocr_tesseract_cmd", "tesseract"),
            language=getattr(settings, "pdf_ocr_language", "eng"),
            timeout_seconds=max(1, int(getattr(settings, "pdf_ocr_timeout_seconds", 60))),
        )
    if engine == "rapidocr":
        return RapidOcrProvider()
    raise RuntimeError(
        f"Unsupported PDF OCR engine `{engine}`. Choose `tesseract` or `rapidocr`."
    )


def get_ocr_runtime_status(settings: Settings) -> dict[str, object]:
    enabled = bool(getattr(settings, "pdf_ocr_enabled", True))
    engine = normalize_ocr_engine(getattr(settings, "pdf_ocr_engine", "tesseract"))
    if not enabled:
        return {
            "enabled": False,
            "engine": engine,
            "available": False,
            "detail": "PDF OCR is disabled.",
        }

    if engine == "tesseract":
        configured = str(
            getattr(settings, "pdf_ocr_tesseract_cmd", "tesseract")
        ).strip() or "tesseract"
        try:
            resolve_tesseract_command(configured)
        except RuntimeError as exc:
            return {
                "enabled": True,
                "engine": engine,
                "available": False,
                "detail": str(exc),
            }
        return {
            "enabled": True,
            "engine": engine,
            "available": True,
            "detail": "Tesseract OCR is installed.",
        }

    if engine == "rapidocr":
        rapidocr_available = importlib.util.find_spec("rapidocr") is not None
        onnx_available = importlib.util.find_spec("onnxruntime") is not None
        available = rapidocr_available and onnx_available
        detail = (
            "RapidOCR and ONNX Runtime are installed."
            if available
            else "Install `requirements-rapidocr.txt` to use the RapidOCR engine."
        )
        return {
            "enabled": True,
            "engine": engine,
            "available": available,
            "detail": detail,
        }

    return {
        "enabled": True,
        "engine": engine,
        "available": False,
        "detail": f"Unsupported PDF OCR engine `{engine}`.",
    }


def normalize_ocr_engine(value: Any) -> str:
    return str(value or "tesseract").strip().lower() or "tesseract"


def resolve_tesseract_command(configured: str) -> str:
    resolved = shutil.which(configured)
    if resolved:
        return resolved

    configured_path = Path(configured).expanduser()
    if configured_path.is_file():
        return str(configured_path.resolve())

    raise RuntimeError(
        "PDF OCR requires Tesseract OCR. Install Tesseract and add it to PATH, "
        "set PDF_OCR_TESSERACT_CMD to the executable path, or select RapidOCR."
    )


def _main() -> int:
    from app.config import get_settings

    status = get_ocr_runtime_status(get_settings())
    print(status["detail"])
    return 0 if not status["enabled"] or status["available"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
