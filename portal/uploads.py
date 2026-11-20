from pathlib import Path
from contextlib import contextmanager
import logging
from zipfile import BadZipFile, ZipFile
from django.core.exceptions import ValidationError
from django.db.models import FileField


class UploadTracker:
    def __init__(self):
        self.files = []

    def save(self, instance, **kwargs):
        for field in instance._meta.fields:
            if isinstance(field, FileField):
                value = getattr(instance, field.name)
                if value and not value._committed:
                    self.files.append(value)
        instance.save(**kwargs)


@contextmanager
def rollback_uploads():
    """置于 atomic 外层，仅在事务失败时清理本次新上传的文件。"""
    tracker = UploadTracker()
    try:
        yield tracker
    except BaseException:
        for field_file in reversed(tracker.files):
            if field_file._committed:
                try:
                    field_file.storage.delete(field_file.name)
                except Exception:
                    logging.getLogger(__name__).exception("清理失败上传文件时出错：%s", field_file.name)
        raise

GUIDE_FILE_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
GUIDE_MAX_ATTACHMENTS = 10
GUIDE_MAX_FILE_BYTES = 20 * 1024 * 1024
REGISTRATION_FILE_TYPES = {**GUIDE_FILE_TYPES, ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
NOTIFICATION_FILE_TYPES = {
    **REGISTRATION_FILE_TYPES,
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".zip": "application/zip",
}
NOTIFICATION_MAX_FILE_BYTES = 20 * 1024 * 1024


def validate_notification_upload(upload):
    extension = Path(upload.name).suffix.lower()
    if extension not in NOTIFICATION_FILE_TYPES:
        raise ValidationError("通知附件支持 Word、Excel、PDF、JPG/PNG/GIF/WebP/BMP 图片、PPT、TXT、CSV 或 ZIP 文件。")
    if not upload.size or upload.size > NOTIFICATION_MAX_FILE_BYTES:
        raise ValidationError("通知附件不能为空，且不能超过 20 MB。")
    if extension in REGISTRATION_FILE_TYPES:
        return validate_registration_upload(upload)
    try:
        upload.seek(0)
        head = upload.read(12)
        if extension == ".gif":
            valid = head.startswith((b"GIF87a", b"GIF89a"))
        elif extension == ".webp":
            valid = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
        elif extension == ".bmp":
            valid = head.startswith(b"BM")
        elif extension == ".ppt":
            valid = head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" and upload.size >= 512
        elif extension in {".pptx", ".zip"}:
            upload.seek(0)
            try:
                with ZipFile(upload) as package:
                    names = set(package.namelist())
                    valid = extension == ".zip" or {"[Content_Types].xml", "ppt/presentation.xml"}.issubset(names)
            except (BadZipFile, OSError, UnicodeError):
                valid = False
        else:
            upload.seek(0)
            raw = upload.read()
            text = None
            for encoding in (["utf-16"] if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig", "gb18030"]):
                try:
                    text = raw.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            valid = text is not None and all(ord(char) >= 32 or char in "\t\n\r" for char in text)
        if not valid:
            raise ValidationError("附件内容与所选文件格式不符，请重新上传有效文件。")
    finally:
        upload.seek(0)


def validate_application_file_size(upload):
    if upload.size > GUIDE_MAX_FILE_BYTES:
        raise ValidationError("每个附件不能超过 20 MB。")


def validate_registration_upload(upload):
    extension = Path(upload.name).suffix.lower()
    if extension not in REGISTRATION_FILE_TYPES:
        raise ValidationError("申请附件支持 JPG/PNG 图片、Word、Excel 或 PDF 文件。")
    validate_application_file_size(upload)
    if extension in GUIDE_FILE_TYPES:
        return validate_guide_upload(upload)
    try:
        upload.seek(0)
        head = upload.read(8)
        valid = head.startswith(b"\xff\xd8\xff") if extension in {".jpg", ".jpeg"} else head == b"\x89PNG\r\n\x1a\n"
        if not valid:
            raise ValidationError("图片内容与所选格式不符，请上传有效的 JPG 或 PNG 图片。")
    finally:
        upload.seek(0)


def validate_guide_upload(upload):
    extension = Path(upload.name).suffix.lower()
    if extension not in GUIDE_FILE_TYPES:
        raise ValidationError("附件仅支持 Excel（xls、xlsx）、Word（doc、docx）或 PDF。")
    if upload.size > GUIDE_MAX_FILE_BYTES:
        raise ValidationError("每个附件不能超过 20 MB。")
    try:
        upload.seek(0)
        head = upload.read(8)
        if extension == ".pdf":
            valid = head.startswith(b"%PDF-")
        elif extension in {".doc", ".xls"}:
            valid = head == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" and upload.size >= 512
        else:
            upload.seek(0)
            try:
                with ZipFile(upload) as package:
                    names = set(package.namelist())
                    main_part = "word/document.xml" if extension == ".docx" else "xl/workbook.xml"
                    valid = "[Content_Types].xml" in names and main_part in names
            except (BadZipFile, OSError, UnicodeError):
                valid = False
        if not valid:
            raise ValidationError("附件内容与所选文件格式不符，请重新上传有效的 Office 或 PDF 文件。")
    finally:
        upload.seek(0)
