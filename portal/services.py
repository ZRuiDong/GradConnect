import calendar
import csv
import re
import io
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from datetime import timedelta
from django.contrib.auth.hashers import identify_hasher, make_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone
from .forms import (STUDENT_IMPORT_HEADERS, STUDENT_IMPORT_COLUMNS, STUDENT_IMPORT_MAX_BYTES,
                    STUDENT_IMPORT_MAX_ROWS, StudentImportRowForm, StudentUpdateRowForm)
from .models import AttendanceBatch, Audit, Leave, LeaveEvent, MonthlyAttendance, StudentImportBatch, User
from .student_state import STUDENT_PROFILE_COLUMNS, student_profile_digest


def audit(actor, action, obj):
    return Audit.objects.create(actor=actor, action=action, target=f"{obj._meta.label}:{obj.pk}")


@transaction.atomic
def transition_leave(leave_id, actor, action, note=""):
    transitions = {
        "approve": (Leave.Status.PENDING, Leave.Status.APPROVED),
        "reject": (Leave.Status.PENDING, Leave.Status.REJECTED),
        "withdraw": (Leave.Status.PENDING, Leave.Status.WITHDRAWN),
        "close": (Leave.Status.APPROVED, Leave.Status.CLOSED),
    }
    if action not in transitions:
        raise ValidationError("不支持的操作。")
    item = Leave.objects.get(pk=leave_id)
    if action == "withdraw":
        if actor.is_staff or item.student_id != actor.pk:
            raise ValidationError("仅申请人可以撤销本人的待审核申请。")
    elif not actor.is_staff:
        raise ValidationError("仅管理员可以审核和确认销假。")
    if action == "reject" and not note.strip():
        raise ValidationError("驳回时请填写审核意见。")
    if len(note) > 2000:
        raise ValidationError("审核意见不能超过 2000 字。")
    before, after = transitions[action]
    # 条件更新同时适用于 SQLite 和 PostgreSQL，防止审核与撤销互相覆盖。
    changed = Leave.objects.filter(pk=leave_id, status=before).update(status=after, updated_at=timezone.now())
    if not changed:
        raise ValidationError("申请状态已经变化，请刷新后重试。")
    LeaveEvent.objects.create(leave=item, actor=actor, status=after, note=note.strip())
    from .inbox import deliver
    deliver('leave', item, actor, f'leave:{item.pk}:{after}',
        f'请假申请 #{item.pk}：{Leave.Status(after).label}', to_admins=action == 'withdraw')
    audit(actor, f"请假：{action}", item)
    return item


def parse_attendance(upload):
    if upload.size > 2 * 1024 * 1024:
        raise ValidationError("请上传不超过 2 MB 的 CSV 文件。")
    try:
        text = upload.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValidationError("请将文件另存为 UTF-8 编码的 CSV。")
    if "\x00" in text:
        raise ValidationError("文件含有无效的空字符，请重新导出 CSV。")
    headers = ["学号", "姓名", "月份", "打卡记录", "请假天数"]
    try:
        reader = csv.DictReader(io.StringIO(text), strict=True)
        if reader.fieldnames != headers:
            raise ValidationError("表头必须依次为：学号、姓名、月份、打卡记录、请假天数。请使用下载的月度模板。")
        users = {user.username: user for user in User.objects.filter(is_staff=False, is_active=True).only("pk", "username", "full_name")}
        rows, errors, seen = [], [], set()
        for line, row in enumerate(reader, 2):
            if line > 5001:
                raise ValidationError("单次最多导入 5000 行，请拆分文件后导入。")
            if None in row or any(value is None for value in row.values()):
                errors.append(f"第 {line} 行：列数不正确。")
                continue
            username, name, month, clock, leave = [row[key].strip() for key in headers]
            username = User.normalize_username(username)
            if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month) or int(month[:4]) < 1900:
                errors.append(f"第 {line} 行：月份应为 YYYY-MM（年份不早于 1900）。")
                continue
            key = (username, month)
            if key in seen:
                errors.append(f"第 {line} 行：同一学号和月份重复。")
                continue
            seen.add(key)
            user = users.get(username)
            if not user:
                errors.append(f"第 {line} 行：学号 {username} 不存在或已停用，请先核对学生账号。")
            elif not name or name != user.full_name.strip():
                errors.append(f"第 {line} 行：姓名为空或与该学号的学生姓名不一致。")
            elif not re.fullmatch(r"[0-9]{1,2}", clock) or not re.fullmatch(r"[0-9]{1,2}", leave):
                errors.append(f"第 {line} 行：打卡记录和请假天数均须填写非负整数天数。")
            else:
                clock_days, leave_days = int(clock), int(leave)
                if clock_days + leave_days > calendar.monthrange(int(month[:4]), int(month[5:]))[1]:
                    errors.append(f"第 {line} 行：打卡天数与请假天数之和不能超过该月实际天数。")
                else:
                    rows.append({"student_id": user.pk, "username": username, "name": name, "month": month,
                                 "clock_days": clock_days, "leave_days": leave_days, "total_days": clock_days + leave_days})
        if errors:
            raise ValidationError(errors[:30] + (["错误过多，仅展示前 30 条。"] if len(errors) > 30 else []))
        if not rows:
            raise ValidationError("文件中没有可导入的月度打卡记录。")
        return rows
    except csv.Error:
        raise ValidationError("CSV 格式有误，请重新导出后上传。")


@transaction.atomic
def confirm_attendance(batch_id, actor):
    if not actor.is_active or not actor.is_staff:
        raise ValidationError("仅管理员可以导入打卡。")
    try:
        batch = AttendanceBatch.objects.select_for_update().get(pk=batch_id)
    except AttendanceBatch.DoesNotExist:
        raise ValidationError("预览已取消或被清理，请重新上传并核对。") from None
    if batch.uploaded_by_id != actor.pk:
        raise ValidationError("仅上传人可以确认本次导入。")
    if batch.is_confirmed:
        raise ValidationError("该批次已经导入，请勿重复确认。")
    if batch.format_version != 2:
        raise ValidationError("这是旧版每日打卡预览，请重新使用月度模板上传。")
    if batch.created_at < timezone.now() - timedelta(days=1):
        raise ValidationError("导入预览已过期，请重新上传。")
    if not batch.payload:
        raise ValidationError("没有可确认的月度记录，请重新上传。")
    ids = {row["student_id"] for row in batch.payload}
    users = {user.pk: user for user in User.objects.select_for_update().filter(pk__in=ids, is_staff=False, is_active=True)}
    if any(row["student_id"] not in users or users[row["student_id"]].username != row["username"] or
           users[row["student_id"]].full_name.strip() != row["name"] for row in batch.payload):
        raise ValidationError("预览后有学生账号或姓名发生变化，请重新上传。")
    records = [MonthlyAttendance(student_id=row["student_id"], student_number=row["username"], student_name=row["name"],
                month=date.fromisoformat(row["month"] + "-01"), clock_days=row["clock_days"], leave_days=row["leave_days"], batch=batch)
               for row in batch.payload]
    MonthlyAttendance.objects.bulk_create(records, batch_size=500, update_conflicts=True,
        unique_fields=["student", "month"], update_fields=["student_name", "student_number", "clock_days", "leave_days", "batch", "updated_at"])
    batch.is_confirmed = True
    batch.payload = []
    batch.save(update_fields=["is_confirmed", "payload"])
    audit(actor, "确认导入月度打卡", batch)
    return batch


def existing_account_names():
    return {User.normalize_username(name).casefold() for name in User.objects.values_list("username", flat=True)}


def prepare_student_account_import(upload, mode="create"):
    """先校验整份文件，再按原始顺序提供可安全保存的预览数据块。"""
    if upload.size > STUDENT_IMPORT_MAX_BYTES:
        raise ValidationError("请上传不超过 5 MB 的 CSV 文件。")
    try:
        text = upload.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValidationError("请将文件另存为 UTF-8 编码的 CSV。")
    if "\x00" in text:
        raise ValidationError("文件含有无效的空字符，请重新导出 CSV。")
    errors, rows, seen = [], [], set()
    if mode not in {"create", "update"}:
        raise ValidationError("请选择有效导入方式。")
    current_users = {user.username: user for user in User.objects.filter(is_staff=False, is_superuser=False)} if mode == "update" else {}
    existing = existing_account_names() if mode == "create" else set()
    political_labels = {label: code for code, label in User.Politics.choices}
    political_labels.update({code: code for code in User.Politics.values})
    political_labels.update({"（群众）": "mass", "(群众)": "mass", "预备": "probationary", "": ""})
    try:
        reader = csv.DictReader(io.StringIO(text), strict=True)
        # 兼容先前下载的九列模板，院系和所在班级始终是两个独立字段。
        headers = ["院系" if header == "院系/班级" else header for header in reader.fieldnames or []]
        reader.fieldnames = headers
        if mode == "create" and headers not in (STUDENT_IMPORT_HEADERS, [h for h in STUDENT_IMPORT_HEADERS if h != "所在班级"]):
            raise ValidationError("请使用学生信息模板的表头顺序，可省略“所在班级”列。")
        if mode == "update" and (len(set(headers)) != len(headers) or "学号" not in headers or
                any(header not in STUDENT_IMPORT_HEADERS for header in headers) or
                not any(header in STUDENT_IMPORT_COLUMNS and header != "学号" for header in headers)):
            raise ValidationError("更新表头须包含学号和至少一项待更新资料，不得有重复列或未知列。例如：学号、政治面貌。")
        update_fields = [field for header, field in STUDENT_IMPORT_COLUMNS.items() if header in headers and field != "username"]
        for line, row in enumerate(reader, 2):
            if line > STUDENT_IMPORT_MAX_ROWS + 1:
                raise ValidationError(f"单次最多导入 {STUDENT_IMPORT_MAX_ROWS} 人，请拆分文件后分批导入。")
            if None in row or any(value is None for value in row.values()):
                errors.append(f"第 {line} 行：列数不正确。")
                continue
            username = User.normalize_username(row["学号"].strip())
            password = row.get("初始密码", "")
            key = username.casefold()
            if key in seen:
                errors.append(f"第 {line} 行：学号与文件中的其他行重复。")
                continue
            seen.add(key)
            if key in existing:
                errors.append(f"第 {line} 行：该学号已存在，请从文件中移除；已有账号不会被覆盖。")
                continue
            if mode == "update" and (username not in current_users or password):
                errors.append(f"第 {line} 行：更新资料须使用已有学生学号，并将初始密码留空。")
                continue
            instance = current_users.get(username) if mode == "update" else None
            source_digest = student_profile_digest(instance) if instance else None
            snapshot = {field: getattr(instance, field) for field in STUDENT_PROFILE_COLUMNS} if instance else {}
            if len(password) > 128:
                errors.append(f"第 {line} 行：初始密码不能超过 128 个字符。")
                continue
            data = {field: row[header].strip() for header, field in STUDENT_IMPORT_COLUMNS.items() if header in row}
            data["username"] = username
            if "political_status" in data:
                data["political_status"] = political_labels.get(data["political_status"], data["political_status"])
            if "enrollment_status" in data:
                data["enrollment_status"] = dict((label, code) for code, label in User.EnrollmentStatus.choices).get(data["enrollment_status"], data["enrollment_status"])
            if mode == "update":
                form = StudentUpdateRowForm(data=data, instance=instance, update_fields=update_fields)
            else:
                form = StudentImportRowForm(data={**data, "password1": password, "password2": password})
            if not form.is_valid():
                for field, field_errors in form.errors.items():
                    label = "初始密码" if field in {"password1", "password2"} else form.fields[field].label if field in form.fields else "账号"
                    for error in field_errors:
                        errors.append(f"第 {line} 行：{label}：{error}")
                continue
            profile = {**snapshot, **{field: form.cleaned_data[field] for field in STUDENT_PROFILE_COLUMNS if field in form.cleaned_data}}
            rows.append({"username": form.instance.username, **profile, "password": password,
                         **({"student_id": instance.pk, "source_digest": source_digest, "update_fields": update_fields} if instance else {})})
    except csv.Error:
        raise ValidationError("CSV 格式有误，请重新导出后上传。")
    if errors:
        raise ValidationError(errors[:30] + (["错误过多，仅展示前 30 条。"] if len(errors) > 30 else []))
    if not rows:
        raise ValidationError("文件中没有可导入的学生账号。")
    if mode == "update":
        for row in rows:
            row.pop("password", None)
        return len(rows), (rows,)
    return len(rows), _hash_student_chunks(rows)


# 工作线程按需启动，同一进程内同时导入的批次共用并行额度。
_available_cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 1
_hash_workers = min(_available_cpus, max(2, _available_cpus - 1), 4)
_student_hash_pool = ThreadPoolExecutor(max_workers=_hash_workers, thread_name_prefix="student-import")


def _hash_student_chunks(rows):
    def encode_row(row):
        row["password_hash"] = make_password(row.pop("password"))
        return row

    # 每次只调度一小块，断开流后不会继续计算整份名单。
    chunk_size = _hash_workers * 4
    for start in range(0, len(rows), chunk_size):
        yield list(_student_hash_pool.map(encode_row, rows[start:start + chunk_size]))


def parse_student_accounts(upload, mode="create"):
    _, chunks = prepare_student_account_import(upload, mode)
    return [row for chunk in chunks for row in chunk]


def confirm_student_accounts(batch_id, actor):
    if not actor.is_active or not actor.is_staff:
        raise ValidationError("仅管理员可以导入学生账号。")
    try:
        with transaction.atomic():
            try:
                batch = StudentImportBatch.objects.select_for_update().get(pk=batch_id, uploaded_by=actor)
            except StudentImportBatch.DoesNotExist:
                raise ValidationError("预览已取消或被清理，请重新上传并核对。") from None
            if batch.is_confirmed:
                raise ValidationError("该批次已经导入，请勿重复确认。")
            if batch.format_version != 3:
                raise ValidationError("这是旧版账号导入预览，请使用新的学生信息模板重新上传。")
            if batch.created_at < timezone.now() - timedelta(days=1):
                raise ValidationError("导入预览已超过一天，请重新上传。")
            if not batch.payload or len(batch.payload) != batch.row_count or batch.row_count > STUDENT_IMPORT_MAX_ROWS:
                raise ValidationError("预览数据已失效，请重新上传。")
            if batch.mode not in {"create", "update"}:
                raise ValidationError("预览导入方式无效，请重新上传。")
            existing = existing_account_names() if batch.mode == "create" else set()
            current = {user.pk: user for user in User.objects.select_for_update().filter(
                pk__in=[row.get("student_id") for row in batch.payload], is_staff=False, is_superuser=False)} if batch.mode == "update" else {}
            seen, users = set(), []
            profile_fields = list(STUDENT_PROFILE_COLUMNS)
            if batch.mode == "update":
                profile_fields = batch.payload[0].get("update_fields", profile_fields)
                if (not isinstance(profile_fields, list) or not profile_fields or
                        any(not isinstance(field, str) or field not in STUDENT_PROFILE_COLUMNS for field in profile_fields) or
                        len(set(profile_fields)) != len(profile_fields)):
                    raise ValidationError("预览更新字段无效，请重新上传。")
            for row in batch.payload:
                required = set(User.REQUIRED_STUDENT_FIELDS)
                if batch.mode == "update":
                    required &= {"username", *profile_fields}
                    if row.get("update_fields", list(STUDENT_PROFILE_COLUMNS)) != profile_fields or any(field not in row for field in profile_fields):
                        raise ValidationError("预览更新字段不一致，请重新上传。")
                if not all(row.get(field) for field in required):
                    raise ValidationError("预览资料不完整，请使用新的学生信息模板重新上传。")
                key = User.normalize_username(row["username"]).casefold()
                if key in existing or key in seen:
                    raise ValidationError("预览后有学号被创建或出现重复。本次未导入任何账号，请重新上传。")
                seen.add(key)
                if batch.mode == "update":
                    user = current.get(row.get("student_id"))
                    if not user or user.username != row["username"]:
                        raise ValidationError("预览后有学生账号发生变化，本次未更新任何资料，请重新上传。")
                    if row.get("source_digest") != student_profile_digest(user):
                        raise ValidationError("预览后有学生资料已被修改或预览版本已过期，本次未更新任何资料，请重新上传并核对。")
                else:
                    encoded = row.get("password_hash", "")
                    try:
                        identify_hasher(encoded)
                    except ValueError:
                        raise ValidationError("预览密码数据已失效，请重新上传。")
                    user = User(username=row["username"], password=encoded, is_staff=False, is_superuser=False,
                        is_active=True, is_archived=False, password_change_required=True)
                for field in profile_fields:
                    setattr(user, field, row.get(field, ""))
                excluded = [field.name for field in User._meta.fields if field.name not in {"username", *profile_fields}] if batch.mode == "update" else None
                user.full_clean(exclude=excluded, validate_unique=False)
                users.append(user)
            if batch.mode == "update":
                User.objects.bulk_update(users, profile_fields, batch_size=200)
            else:
                # 显式设置角色，不接受 CSV 或请求参数中的任何权限字段。
                User.objects.bulk_create(users, batch_size=200)
            batch.is_confirmed = True
            batch.confirmed_at = timezone.now()
            batch.payload = []
            batch.save(update_fields=["is_confirmed", "confirmed_at", "payload"])
            audit(actor, f"{'批量更新学生资料' if batch.mode == 'update' else '批量创建学生账号'}：{batch.row_count} 人", batch)
            return batch
    except IntegrityError:
        # 例如另一名管理员在预览与确认之间创建了相同学号；事务整批回滚。
        raise ValidationError("账号发生冲突，本次未导入任何账号。请刷新列表并重新上传。")
    except OperationalError as exc:
        if "locked" in str(exc).lower():
            raise ValidationError("系统正在处理其他导入，请稍后重试；本次操作未写入账号。")
        raise
