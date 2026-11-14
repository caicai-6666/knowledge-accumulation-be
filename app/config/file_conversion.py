"""Office 程序定位与 PDF 页面渲染预算；不读取模型生成额度。"""
import os
from pathlib import Path
from dotenv import dotenv_values
from app.config.middleware import PROJECT_ROOT


def load_office_converter_executable() -> str | None:
    values = {**dotenv_values(PROJECT_ROOT / '.env', interpolate=False), **os.environ}
    return (values.get('OFFICE_CONVERTER_EXECUTABLE') or '').strip() or None


def load_pdf_visual_token_budget(env_file: Path | None = None) -> int:
    values = {**dotenv_values(env_file if env_file is not None else PROJECT_ROOT / '.env',
                             interpolate=False), **os.environ}
    raw = values.get('PDF_MAX_VISUAL_TOKENS_PER_PAGE', '2048')
    try:
        budget = int(raw)
    except (TypeError, ValueError):
        raise ValueError('PDF_MAX_VISUAL_TOKENS_PER_PAGE 必须为正整数') from None
    if budget <= 0:
        raise ValueError('PDF_MAX_VISUAL_TOKENS_PER_PAGE 必须为正整数')
    return budget
