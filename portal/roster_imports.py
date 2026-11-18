"""只读解析 Excel 名单；修改学生资料必须另行确认整个预览批次。"""
import io
import re
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from zipfile import BadZipFile, ZipFile
from xml.etree.ElementTree import ParseError
from struct import error as StructError
import openpyxl
import xlrd
from defusedxml.common import DefusedXmlException
from openpyxl.utils.exceptions import InvalidFileException
from xlrd.compdoc import CompDocError
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from .forms import CadreTenureImportForm
from .models import Audit, CadreTenure, StudentRosterBatch, User
from .student_state import student_edit_digest, student_profile_digest


MAX_ROWS = 2000
MAX_BYTES = 5 * 1024 * 1024
DEFAULT_END = date(2999, 12, 31)
HEADERS = {"graduate": ["学号", "姓名"], "tenure": ["学号", "姓名", "任职职务", "开始日期", "结束日期"]}


def _text(value, number_format=""):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value == int(value) and abs(value) < 10**15:
        result = str(int(value))
        return result.zfill(len(number_format)) if re.fullmatch(r"0+", number_format) else result
    raise ValidationError("学号、姓名和职务须为文本；学号请用文本格式保留前导零，不能使用小数或超长数字。")


def _excel_rows(upload, kind):
    if kind not in HEADERS or Path(upload.name).suffix.lower() not in {".xlsx", ".xls"} or upload.size > MAX_BYTES:
        raise ValidationError("请上传不超过5 MB的Excel文件（.xlsx或.xls）。")
    raw = upload.read()
    result = []
    try:
        if Path(upload.name).suffix.lower() == ".xlsx":
            with ZipFile(io.BytesIO(raw)) as package:
                if sum(item.file_size for item in package.infolist()) > 40 * 1024 * 1024:
                    raise ValidationError("Excel解压后体积过大，请移除无关内容后重试。")
            book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
            try:
                if len([sheet for sheet in book.worksheets if sheet.title != "填写说明"]) != 1:
                    raise ValidationError("请仅保留一张名单工作表（可保留模板中的“填写说明”页）。")
                sheet = next(sheet for sheet in book.worksheets if sheet.title != "填写说明")
                if ((sheet.max_row is not None and sheet.max_row > MAX_ROWS + 1)
                        or (sheet.max_column is not None and sheet.max_column > len(HEADERS[kind]))):
                    raise ValidationError("表格范围超出模板，请删除多余行列；单次最多2000行。")
                # Some valid producers omit dimensions; others report a smaller range
                # than their actual data. Stream actual rows with our own hard limits.
                sheet.reset_dimensions()
                for line, cells in enumerate(sheet.iter_rows(), 1):
                    if line > MAX_ROWS + 1 or len(cells) > len(HEADERS[kind]):
                        raise ValidationError("表格范围超出模板，请删除多余行列；单次最多2000行。")
                    if any(cell.data_type in {"f", "e"} for cell in cells):
                        raise ValidationError("名单不能包含公式或错误值，请粘贴为实际值后重试。")
                    result.append([(cell.value, cell.number_format) for cell in cells]
                        + [(None, "General")] * (len(HEADERS[kind])-len(cells)))
            finally:
                book.close()
        else:
            book = xlrd.open_workbook(file_contents=raw, on_demand=True)
            try:
                sheets = [sheet for sheet in book.sheets() if sheet.name != "填写说明"]
                if len(sheets) != 1:
                    raise ValidationError("请仅保留一张名单工作表。")
                sheet = sheets[0]
                if sheet.nrows > MAX_ROWS + 1 or sheet.ncols > len(HEADERS[kind]):
                    raise ValidationError("表格范围超出模板；单次最多2000行。")
                for row in range(sheet.nrows):
                    values = []
                    for cell in sheet.row(row):
                        if cell.ctype == xlrd.XL_CELL_ERROR:
                            raise ValidationError("名单包含Excel错误值，请修正后重试。")
                        value = xlrd.xldate_as_datetime(cell.value, book.datemode) if cell.ctype == xlrd.XL_CELL_DATE else cell.value
                        values.append((value, ""))
                    result.append(values)
            finally:
                book.release_resources()
    except ValidationError:
        raise
    except (BadZipFile, OSError, ValueError, KeyError, IndexError, EOFError, StopIteration, TypeError, OverflowError,
            NotImplementedError, DefusedXmlException, xlrd.XLRDError, CompDocError, ParseError, StructError, InvalidFileException):
        raise ValidationError("无法读取Excel，请使用有效的模板文件，移除密码保护后重试。")
    if not result or [_text(value) for value, _ in result[0]] != HEADERS[kind]:
        raise ValidationError("第一行表头必须依次为：" + "、".join(HEADERS[kind]) + "。")
    return result[1:]


def parse_roster(upload, kind):
    source = _excel_rows(upload, kind)
    names = set()
    for cells in source:
        try:
            names.add(User.normalize_username(_text(*cells[0])))
        except (ValidationError, IndexError):
            pass  # The detailed row validation below reports these errors.
    query = User.objects.filter(is_staff=False, is_superuser=False, username__in=names)
    users = {user.username: user for user in (query.prefetch_related("cadre_tenures") if kind == "tenure" else query)}
    errors, payload, seen = [], [], set()
    existing = {(item.student_id, item.position, item.starts_on): item for user in users.values() for item in user.cadre_tenures.all()} if kind == "tenure" else {}
    for line, cells in enumerate(source, 2):
        if all(value is None or isinstance(value, str) and not value.strip() for value, _ in cells):
            continue
        try:
            values = [value for value, _ in cells]
            username = User.normalize_username(_text(*cells[0]))
            name = _text(values[1])
            student = users.get(username)
            if not student or not name or name != student.full_name.strip():
                raise ValidationError("学号不存在、不是学生账号，或姓名与账号不一致。")
            row = {"student_id": student.pk, "username": username, "name": name,
                   "source_digest": student_edit_digest(student) if kind == "tenure" else student_profile_digest(student),
                   "original_status": student.enrollment_status}
            key = username
            if kind == "tenure":
                position = _text(values[2])
                def date_text(value):
                    return value.date().isoformat() if isinstance(value, datetime) else value.isoformat() if isinstance(value, date) else str(value or "").strip()
                form = CadreTenureImportForm(data={"position": position, "starts_on": date_text(values[3]), "ends_on": date_text(values[4])})
                if not form.is_valid():
                    raise ValidationError([str(error) for errors_for_field in form.errors.values() for error in errors_for_field])
                start, end = form.cleaned_data["starts_on"], form.cleaned_data["ends_on"]
                key = (username, position, start)
                matches = [item for item in student.cadre_tenures.all() if item.position == position and item.starts_on == start]
                if len(matches) > 1:
                    raise ValidationError("历史中存在相同职务及开始日期的重复记录，请先在学生详情中核对。")
                old = existing.get((student.pk, position, start))
                row.update(position=position, starts_on=start.isoformat(), ends_on=end.isoformat(),
                           tenure_id=old.pk if old else None, action="更新结束日期" if old else "新增履职记录")
            else:
                row["action"] = "已毕业，无需重复修改" if student.enrollment_status == User.EnrollmentStatus.GRADUATED else "在校 → 毕业"
            if key in seen:
                raise ValidationError("名单中出现重复学号，或重复的学号/职务/开始日期。")
            seen.add(key)
            payload.append(row)
        except (ValidationError, IndexError) as exc:
            messages = exc.messages if isinstance(exc, ValidationError) else ["列数不正确，请重新下载模板。"]
            errors.extend(f"第{line}行：{message}" for message in messages)
    if kind == "tenure":
        new_counts = {}
        for row in payload:
            if not row["tenure_id"]:
                new_counts[row["student_id"]] = new_counts.get(row["student_id"], 0) + 1
        for student in users.values():
            if len(student.cadre_tenures.all()) + new_counts.get(student.pk, 0) > 100:
                errors.append(f"学号{student.username}：导入后履职记录超过100条。")
    if errors:
        raise ValidationError(errors[:30] + (["错误过多，仅展示前30条。"] if len(errors) > 30 else []))
    if not payload:
        raise ValidationError("没有可导入的名单，请在表头下面填写学生资料。")
    return payload


@transaction.atomic
def confirm_roster(batch_id, actor):
    if not actor.is_active or not actor.is_staff:
        raise ValidationError("仅管理员可以批量修改学生信息。")
    try:
        batch = StudentRosterBatch.objects.select_for_update().get(pk=batch_id, uploaded_by=actor)
    except StudentRosterBatch.DoesNotExist:
        raise ValidationError("预览已取消或被清理，请重新上传并核对。") from None
    if batch.is_confirmed:
        raise ValidationError("该批次已经处理，请勿重复确认。")
    if batch.created_at < timezone.now() - timedelta(days=1) or not batch.payload or len(batch.payload) != batch.row_count or batch.row_count > MAX_ROWS:
        raise ValidationError("预览已过期或失效，请重新上传。")
    query = User.objects.select_for_update().filter(pk__in={row["student_id"] for row in batch.payload}, is_staff=False, is_superuser=False)
    users = {user.pk: user for user in (query.prefetch_related("cadre_tenures") if batch.kind == "tenure" else query)}
    for row in batch.payload:
        student = users.get(row["student_id"])
        digest = student_edit_digest(student) if student and batch.kind == "tenure" else student_profile_digest(student) if student else None
        if not student or student.username != row["username"] or digest != row["source_digest"]:
            raise ValidationError("预览后有学生资料或履职记录发生变化，本次整批未修改，请重新上传并核对。")
    if batch.kind == "graduate":
        changed = User.objects.filter(pk__in=users, enrollment_status=User.EnrollmentStatus.ENROLLED).update(enrollment_status=User.EnrollmentStatus.GRADUATED)
    elif batch.kind == "tenure":
        creates, updates = [], []
        old = {item.pk: item for item in CadreTenure.objects.select_for_update().filter(
            pk__in=[row["tenure_id"] for row in batch.payload if row["tenure_id"]])}
        saved_at = timezone.now()
        for row in batch.payload:
            start, end = date.fromisoformat(row["starts_on"]), date.fromisoformat(row["ends_on"])
            if row["tenure_id"]:
                item = old.get(row["tenure_id"])
                if not item or (item.student_id, item.position, item.starts_on) != (row["student_id"], row["position"], start):
                    raise ValidationError("履职记录已变化，请重新上传并核对。")
                item.ends_on, item.updated_at = end, saved_at
                updates.append(item)
            else:
                creates.append(CadreTenure(student_id=row["student_id"], position=row["position"], starts_on=start, ends_on=end))
        CadreTenure.objects.bulk_create(creates, batch_size=200)
        if updates:
            CadreTenure.objects.bulk_update(updates, ["ends_on", "updated_at"], batch_size=200)
        changed = len(batch.payload)
    else:
        raise ValidationError("导入类型无效。")
    Audit.objects.bulk_create([Audit(actor=actor, action="批量设置毕业" if batch.kind == "graduate" else "批量导入履职记录", target=f"portal.User:{user.pk}") for user in users.values()], batch_size=500)
    batch.is_confirmed, batch.confirmed_at, batch.payload = True, timezone.now(), []
    batch.save(update_fields=["is_confirmed", "confirmed_at", "payload"])
    return changed
