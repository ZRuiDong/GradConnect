"""Student presence and daily aggregates; no raw navigation or playback log."""
import calendar
import logging
from datetime import date, timedelta
from django import forms
from django.db import DatabaseError, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone
from .models import ContentVisitDaily, StudentDailyActivity, User


logger = logging.getLogger(__name__)
ONLINE_WINDOW = timedelta(minutes=5)
WRITE_INTERVAL = timedelta(seconds=60)


def eligible_student(user):
    return user.is_authenticated and user.is_active and not user.is_staff and not user.is_superuser and not user.is_archived


def speculative_request(request):
    purpose = request.headers.get("Sec-Purpose", "") + request.headers.get("Purpose", "")
    return any(word in purpose.lower() for word in ["prefetch", "prerender"])


def touch_activity(user, now=None):
    if not eligible_student(user):
        return
    now = now or timezone.now()
    today = timezone.localdate(now)
    if (user.last_seen_at and now-WRITE_INTERVAL < user.last_seen_at <= now
            and timezone.localdate(user.last_seen_at) == today and user.online_until and user.online_until > now):
        return
    try:
        with transaction.atomic():
            # Conditional writes throttle across tabs and server workers, not just this process.
            changed = User.objects.filter(pk=user.pk, is_active=True, is_archived=False, is_staff=False, is_superuser=False).filter(
                Q(last_seen_at__isnull=True) | Q(last_seen_at__lte=now-WRITE_INTERVAL) | Q(last_seen_at__date__lt=today)
                | Q(online_until__isnull=True) | Q(online_until__lte=now)
            ).update(last_seen_at=now, online_until=now+ONLINE_WINDOW)
            if changed:
                entry, created = StudentDailyActivity.objects.get_or_create(student=user, day=today,
                    defaults={"first_seen_at": now, "last_seen_at": now})
                if not created:
                    StudentDailyActivity.objects.filter(pk=entry.pk, last_seen_at__lt=now).update(last_seen_at=now)
    except DatabaseError:
        logger.exception("记录学生活跃状态失败")


def record_content_click(request, kind, object_id):
    if request.method != "GET" or speculative_request(request) or not eligible_student(request.user):
        return
    now = timezone.now()
    try:
        with transaction.atomic():
            row, _ = ContentVisitDaily.objects.get_or_create(student=request.user, kind=kind,
                object_id=object_id, day=timezone.localdate(now))
            ContentVisitDaily.objects.filter(pk=row.pk).update(views=F("views")+1, last_visited_at=now)
    except DatabaseError:
        logger.exception("记录内容点击量失败")


def aggregate_metrics(queryset, today=None):
    today = today or timezone.localdate()
    values = queryset.filter(day__lte=today).aggregate(total=Sum("views"),
        month=Sum("views", filter=Q(day__gte=today.replace(day=1))), today=Sum("views", filter=Q(day=today)))
    return {key: value or 0 for key, value in values.items()}


def detail_metrics(request, kind, item):
    record_content_click(request, kind, item.pk)
    return {"content_metrics": aggregate_metrics(ContentVisitDaily.objects.filter(kind=kind, object_id=item.pk))}


def attach_content_metrics(page, kind):
    if kind not in ContentVisitDaily.Kind.values:
        return
    ids = [item.pk for item in page]
    today = timezone.localdate()
    values = ContentVisitDaily.objects.filter(kind=kind, object_id__in=ids, day__lte=today).order_by().values("object_id").annotate(
        total=Sum("views"), month=Sum("views", filter=Q(day__gte=today.replace(day=1))), today=Sum("views", filter=Q(day=today)))
    metrics = {row["object_id"]: {key: row[key] or 0 for key in ["total", "month", "today"]} for row in values}
    for item in page:
        item.content_metrics = metrics.get(item.pk, {"total": 0, "month": 0, "today": 0})


class ActivityMonthForm(forms.Form):
    month = forms.DateField(label="活跃月份", input_formats=["%Y-%m"],
        widget=forms.DateInput(format="%Y-%m", attrs={"type": "month", "min": "1900-01"}))

    def clean_month(self):
        value = self.cleaned_data["month"].replace(day=1)
        if value.year < 1900 or value > timezone.localdate().replace(day=1):
            raise forms.ValidationError("请选择1900年至本月之间的月份。")
        return value


def student_activity_context(request, student):
    today = timezone.localdate()
    form = ActivityMonthForm({"month": request.GET.get("month", today.strftime("%Y-%m"))})
    valid = form.is_valid()
    month = form.cleaned_data["month"] if valid else today.replace(day=1)
    length = calendar.monthrange(month.year, month.month)[1]
    end = min(today, month.replace(day=length))
    days = set(student.activity_days.filter(day__gte=month, day__lte=end).values_list("day", flat=True)) if valid else set()
    ratio = len(days)/end.day*100 if valid else 0
    rate = round(ratio, 1)
    label = "高活跃" if ratio >= 60 else "中等活跃" if ratio >= 30 else "低活跃" if days else "暂无活跃"
    visits = student.content_visits.filter(day__gte=month, day__lte=end) if valid else student.content_visits.none()
    kinds = dict(visits.order_by().values_list("kind").annotate(n=Sum("views")))
    calendar_days = [{"day": day, "active": date(month.year, month.month, day) in days,
        "future": day > end.day} for day in range(1, length+1)]
    return {"activity_form": form, "student_activity": {"valid": valid, "month": month, "days": len(days),
        "elapsed": end.day, "rate": rate, "label": label, "clicks": sum(kinds.values()),
        "kinds": [{"label": text, "count": kinds.get(key, 0)} for key, text in ContentVisitDaily.Kind.choices],
        "calendar": calendar_days, "offset": range(month.weekday())}}
