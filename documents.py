"""레포 프로파일: 텍스트는 본문, 이미지는 이미지, 문서는 input_file로 전달."""

import base64
import binascii
from pathlib import Path
from core.errors import AnalysisError
from schemas.chat import UploadedFile

SUPPORTED_FILES = {".txt", ".md", ".json", ".csv", ".tsv", ".html", ".xml", ".py", ".js", ".ts",
                   ".pdf", ".doc", ".docx", ".rtf", ".odt", ".ppt", ".pptx", ".xls", ".xlsx",
                   ".png", ".jpg", ".jpeg", ".webp", ".gif"}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
TEXT_FILES = {".txt", ".md", ".json", ".csv", ".tsv", ".html", ".xml", ".py", ".js", ".ts"}


def encoded_files(files):
    return [{"id": f.id, "name": f.name, "mimeType": f.mime_type, "size": len(f.content),
             "contentBase64": base64.b64encode(f.content).decode("ascii")} for f in files]


def decoded_files(files):
    result = []
    for f in files:
        try:
            content = base64.b64decode(f["contentBase64"], validate=True)
        except (ValueError, binascii.Error):
            raise AnalysisError(400, "invalid_file", "원본 파일 인코딩이 올바르지 않아요.") from None
        if len(content) != f["size"]:
            raise AnalysisError(400, "invalid_file", "원본 파일 크기가 일치하지 않아요.")
        result.append(UploadedFile(f["id"], f["name"], f["mimeType"], content))
    if len({f.id for f in result}) != len(result):
        raise AnalysisError(400, "invalid_file", "파일 ID가 중복됐어요.")
    return result


def validate_files(files):
    if len(files) > 10 or sum(len(file.content) for file in files) > 25 * 1024 * 1024:
        raise AnalysisError(400, "files_too_large", "파일은 최대 10개, 전체 25MiB까지 전송해 주세요.")
    for file in files:
        if len(file.content) > 10 * 1024 * 1024 or not file.content:
            raise AnalysisError(400, "invalid_file", "파일은 비어 있지 않아야 하고 각각 10MiB 이하여야 해요.")
        if Path(file.name).suffix.lower() not in SUPPORTED_FILES:
            raise AnalysisError(415, "unsupported_file", "지원하지 않는 파일 형식이에요. 문서·텍스트·PDF·이미지로 전송해 주세요.")



def build_parts(payload, files):
    parts = [{"type": "input_text", "text": payload.model_dump_json()}]
    text_sources = [payload.message, payload.linkContent or ""]
    for file in files:
        suffix = Path(file.name).suffix.lower()
        if suffix in TEXT_FILES:
            try:
                text = file.content.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise AnalysisError(415, "file_encoding", "텍스트 파일은 UTF-8 인코딩으로 저장해 주세요.") from None
            if len(text) > 100_000:
                raise AnalysisError(400, "file_text_too_large", "텍스트 파일 본문은 100000자 이내로 전송해 주세요.")
            parts.append({"type": "input_text", "text": f"file:{file.id}\n파일명: {file.name}\n본문:\n{text}"})
            text_sources.append(text)
        else:
            encoded = base64.b64encode(file.content).decode("ascii")
            parts.append({"type": "input_text", "text": f"다음 첨부는 file:{file.id}, 파일명: {file.name}"})
            if suffix in IMAGE_TYPES:
                parts.append({"type": "input_image", "image_url": f"data:{IMAGE_TYPES[suffix]};base64,{encoded}"})
            else:
                parts.append({"type": "input_file", "filename": file.name,
                              "file_data": f"data:{file.mime_type};base64,{encoded}"})
    return parts, text_sources
