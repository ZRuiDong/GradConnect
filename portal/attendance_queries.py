from datetime import date
from django.db.models import F, Q, Value
from django.db.models.functions import Coalesce, NullIf
from django.utils import timezone
from .forms import AttendanceFilterForm, AttendanceMonthForm, ATTENDANCE_SORT_CHOICES
from .models import MonthlyAttendance


def filtered_attendance(request):
    params = request.GET.copy()
    today = timezone.localdate()
    # 保留 YYYY-MM 及 year/month 旧链接，管理员将其转换为等价年月区间。
    legacy_month = params.get("month", "")
    if (len(legacy_month) == 7 and legacy_month[4] == "-" and legacy_month[5:].isdigit()
            and 1 <= int(legacy_month[5:]) <= 12):
        params.setdefault("year", legacy_month[:4])
        params["month"] = legacy_month[5:].lstrip("0")
    if request.user.is_staff:
        if "month_from" not in params and "month_to" not in params:
            if "year" in params or "month" in params:
                # 先校验旧参数；非法旧链接不可退回默认月份或全部数据。
                legacy_params = params.copy()
                legacy_params.setdefault("year", str(today.year))
                legacy_params.setdefault("month", str(today.month))
                legacy_form = AttendanceMonthForm(legacy_params)
                if legacy_form.is_valid():
                    year = legacy_form.cleaned_data["year"]
                    month = legacy_form.cleaned_data["month"]
                    params["month_from"] = f"{year:04d}-{int(month):02d}" if month else f"{year:04d}-01"
                    params["month_to"] = params["month_from"] if month else f"{year:04d}-12"
                else:
                    params["month_from"] = "invalid"
            else:
                params["month_from"] = params["month_to"] = f"{today.year:04d}-{today.month:02d}"
        params.setdefault("day_kind", "clock_days")
    else:
        params.setdefault("year", str(today.year))
        params.setdefault("month", str(today.month))
    params.setdefault("sort", "month")
    params.setdefault("direction", "desc")
    form = AttendanceFilterForm(params, user=request.user)
    qs = MonthlyAttendance.objects.select_related("student").alias(
        _total_days=F("clock_days") + F("leave_days"),
        _display_name=Coalesce(NullIf("student_name", Value("")), "student_number"))
    if not request.user.is_staff:
        qs = qs.filter(student=request.user)
    if form.is_valid():
        data = form.cleaned_data
        if request.user.is_staff:
            if data["month_from"]:
                qs = qs.filter(month__gte=data["month_from"])
            if data["month_to"]:
                qs = qs.filter(month__lte=data["month_to"])
            day_field = "_total_days" if data["day_kind"] == "total_days" else data["day_kind"]
            if data["days_min"] is not None:
                qs = qs.filter(**{f"{day_field}__gte": data["days_min"]})
            if data["days_max"] is not None:
                qs = qs.filter(**{f"{day_field}__lte": data["days_max"]})
        else:
            year, month = data["year"], data["month"]
            if month:
                qs = qs.filter(month=date(year, int(month), 1))
            else:
                qs = qs.filter(month__gte=date(year, 1, 1), month__lte=date(year, 12, 1))
        if q := data.get("q"):
            qs = qs.filter(Q(student_number__icontains=q) | Q(student_name__icontains=q) |
                           Q(student__username__icontains=q) | Q(student__full_name__icontains=q))
    else:
        qs = qs.none()
    sort_fields = {key: key for key, _ in ATTENDANCE_SORT_CHOICES}
    sort_fields.update(total_days="_total_days", student_name="_display_name")
    selected_sort = form.cleaned_data.get("sort", "month")
    direction = form.cleaned_data.get("direction", "desc")
    field = sort_fields[selected_sort]
    ordering = [("-" if direction == "desc" else "") + field]
    ordering += [item for item in ["-month", "student_number", "pk"] if item.lstrip("-") != field]
    qs = qs.order_by(*ordering)
    return form, qs, params, selected_sort, direction
