"""Whitelist-based sorting shared by full query results and import previews."""
from django.db.models import Case, CharField, F, OuterRef, QuerySet, Subquery, Value, When
from django.db.models.functions import Coalesce, NullIf
from .models import Leave, Registration, User
from .student_services import current_tenures


def col(key, label, field=None, value=None, number=False):
    return {"key": key, "label": label, "field": field or key, "value": value, "number": number}


def get_value(row, key):
    return row.get(key, "") if isinstance(row, dict) else getattr(row, key, "")


def choice_value(key, choices):
    return lambda row: dict(choices).get(get_value(row, key), "未填写")


def columns_for(name):
    identity = [col("name", "姓名", "_sort_name"), col("number", "学号", "_sort_number")]
    dates = [col("starts_at", "开始时间"), col("ends_at", "结束时间")]
    if name == 'manager_list':
        return [col('username', '管理员账号'), col('full_name', '姓名'), col('department', '所属单位'),
            col('email', '邮箱'), col('is_active', '状态'), col('last_login', '最近登录'), col('date_joined', '创建时间')]
    if name == "account_list":
        return [col("username", "学号"), col("full_name", "姓名", "_sort_name"), col("id_number", "身份证号"),
            col("enrollment_status", "在校状态", "_sort_enrollment"), col("party_branch", "党支部"), col("student_class", "班级"),
            col("political_status", "政治面貌", "_sort_politics"), col("position", "当前履职职务", "_sort_position"), col("active", "账号状态", "_sort_active")]
    if name == "leave_list":
        return [col("id", "申请编号", "pk", number=True), *identity, col("kind", "请假类型", "_sort_kind"), *dates, col("status", "状态", "_sort_status")]
    if name in {"internship_list", "rental_list"}:
        extra = col("employer", "实习单位") if name == "internship_list" else col("rental_reason", "租房原因", "_sort_reason")
        return [col("id", "申请编号", "pk", number=True), *identity, extra, *dates, col("status", "状态", "_sort_status")]
    if name == "account_detail":
        return [col("position", "任职职务"), col("starts_on", "开始日期"), col("ends_on", "结束日期")]
    if name == "audit_list":
        return [col("created_at", "时间"), col("actor", "操作人", "_sort_actor"), col("action", "操作"), col("target", "关联记录")]
    if name == "attendance_preview":
        return [col("username", "学号"), col("name", "姓名"), col("month", "月份"), col("clock_days", "打卡天数", number=True),
            col("leave_days", "请假天数", number=True), col("total_days", "总天数", value=lambda row: row.get("clock_days", 0)+row.get("leave_days", 0), number=True)]
    if name == "account_preview":
        return [col("username", "学号"), col("full_name", "姓名"), col("id_number", "身份证号"),
            col("enrollment_status", "在校状态", value=choice_value("enrollment_status", User.EnrollmentStatus.choices)),
            col("party_branch", "党支部"), col("student_class", "班级"),
            col("political_status", "政治面貌", value=choice_value("political_status", User.Politics.choices)), col("department", "院系")]
    if name == "roster_preview":
        return [col("username", "学号"), col("name", "姓名"), col("position", "任职职务"), col("starts_on", "开始日期"),
            col("ends_on", "结束日期"), col("action", "处理方式")]
    if name == "account_delete_confirm":
        return [col("username", "学号"), col("name", "姓名", value=lambda row: row.display_name), col("student_class", "所在班级"),
            col("action", "处理方式", value=lambda row: "归档并停用" if row.will_archive else "彻底删除")]
    return []


def display_choice(field, choices):
    return Case(*[When(**{field: key}, then=Value(label)) for key, label in choices], default=Value("未填写"), output_field=CharField())


def prepare_queryset(qs, name):
    if name == "account_list":
        return qs.alias(_sort_name=Coalesce(NullIf("full_name", Value("")), "username"),
            _sort_enrollment=display_choice("enrollment_status", User.EnrollmentStatus.choices),
            _sort_politics=display_choice("political_status", User.Politics.choices),
            _sort_position=Subquery(current_tenures().filter(student_id=OuterRef("pk")).order_by("-starts_on", "-pk").values("position")[:1]),
            _sort_active=Case(When(is_archived=True, then=Value("已归档")), When(is_active=True, then=Value("正常")), default=Value("已停用"), output_field=CharField()))
    if name in {"leave_list", "internship_list", "rental_list"}:
        qs = qs.alias(_sort_name=Coalesce(NullIf("student_name", Value("")), NullIf("student__full_name", Value("")), "student__username"),
            _sort_number=Coalesce(NullIf("student_number", Value("")), "student__username"),
            _sort_status=display_choice("status", Leave.Status.choices if name == "leave_list" else Registration.Status.choices))
        return qs.alias(_sort_kind=display_choice("kind", Leave.Kind.choices)) if name == "leave_list" else qs.alias(_sort_reason=display_choice("rental_reason", Registration.RentalReason.choices))
    if name == "audit_list":
        return qs.alias(_sort_actor=Coalesce(NullIf("actor__full_name", Value("")), "actor__username", Value("已删除账号")))
    return qs


def sort_for_page(request, records, view_name=None):
    name = view_name or getattr(request.resolver_match, "url_name", "")
    columns = columns_for(name)
    if not columns:
        return records, {}
    mapping = {column["key"]: column for column in columns}
    key, direction = request.GET.get("sort", ""), request.GET.get("direction", "asc")
    error = ""
    if key and (key not in mapping or direction not in {"asc", "desc"}):
        error = "排序参数无效，请使用表头三角按钮重新选择。"
        records = records.none() if isinstance(records, QuerySet) else []
    elif key:
        column = mapping[key]
        if isinstance(records, QuerySet):
            records = prepare_queryset(records, name)
            expression = F(column["field"])
            records = records.order_by(expression.desc(nulls_last=True) if direction == "desc" else expression.asc(nulls_last=True), "pk")
        else:
            def value(row):
                result = column["value"](row) if column["value"] else get_value(row, column["field"])
                if column["number"]:
                    try:
                        return int(result or 0)
                    except (ValueError, TypeError):
                        return 0
                return str(result or "")
            records = sorted(records, key=value, reverse=direction == "desc")
    for column in columns:
        column["active"] = bool(key == column["key"] and not error)
        column["aria_sort"] = ("ascending" if direction == "asc" else "descending") if column["active"] else "none"
        column["links"] = []
        for mode, label in [("asc", "升序"), ("desc", "降序")]:
            params = request.GET.copy()
            params.pop("page", None)
            params["sort"], params["direction"] = column["key"], mode
            column["links"].append({"url": "?"+params.urlencode(), "direction": mode, "label": label,
                "active": column["active"] and mode == direction})
    return records, {"sort_fields": mapping, "table_sort_error": error,
        "table_sort_key": key if key in mapping and not error else "", "table_sort_direction": direction}
