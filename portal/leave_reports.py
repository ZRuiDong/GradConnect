"""请假列表与导出共用查询规则，避免两种结果不一致。"""
import csv
from datetime import date, datetime, time
from django.db.models import Prefetch, Q
from django.utils import timezone
from .models import Leave, LeaveEvent


def filtered_leaves(user, form):
    queryset = Leave.objects.select_related("student").order_by("-created_at", "-pk")
    if not user.is_staff:
        queryset = queryset.filter(student=user)
    if not form.is_valid():
        return queryset.none()
    data = form.cleaned_data
    if data["status"]:
        queryset = queryset.filter(status=data["status"])
    if data["q"]:
        queryset = queryset.filter(Q(student__username__icontains=data["q"]) |
                                  Q(student__full_name__icontains=data["q"]) | Q(student_name__icontains=data["q"]) |
                                  Q(student_number__icontains=data["q"]) | Q(reason__icontains=data["q"]))
    if data["start_date"] and data["start_date"] > date.min:
        beginning = timezone.make_aware(datetime.combine(data["start_date"], time.min))
        queryset = queryset.filter(ends_at__gte=beginning)
    if data["end_date"]:
        ending = timezone.make_aware(datetime.combine(data["end_date"], time.max))
        queryset = queryset.filter(starts_at__lte=ending)
    return queryset


EXPORT_HEADERS = ["申请编号", "学号", "姓名", "院系/班级", "请假类型", "开始时间", "结束时间",
                  "请假原因", "离校去向", "联系电话", "办理状态", "提交时间", "最近审核人", "最近审核时间",
                  "审核意见", "销假确认人", "销假时间", "销假说明", "原申请编号", "去往省份 / 直辖市", "去往城市", "是否导师本人线下签字"]


def csv_text(value):
    """将危险开头作为文字导出，避免表格软件将用户输入当作公式。"""
    text = str(value) if value is not None else ""
    if text.startswith(("\t", "\r", "\n")) or text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def local_timestamp(value):
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S") if value else ""


class CsvBuffer:
    def write(self, value):
        return value


def leave_csv_rows(queryset):
    writer = csv.writer(CsvBuffer())
    yield "\ufeff"
    yield writer.writerow(EXPORT_HEADERS)
    events = LeaveEvent.objects.select_related("actor").order_by("created_at", "pk")
    queryset = queryset.prefetch_related(Prefetch("events", queryset=events, to_attr="report_events"))
    for item in queryset.iterator(chunk_size=200):
        review = next((event for event in reversed(item.report_events)
                       if event.status in {Leave.Status.APPROVED, Leave.Status.REJECTED}), None)
        closed = next((event for event in reversed(item.report_events) if event.status == Leave.Status.CLOSED), None)
        row = [item.pk, item.student_number or item.student.username, item.student_name or item.student.display_name, item.student.department,
               item.get_kind_display(), local_timestamp(item.starts_at), local_timestamp(item.ends_at),
               item.reason, item.destination, item.contact, item.get_status_display(), local_timestamp(item.created_at),
               review.actor.display_name if review else "", local_timestamp(review.created_at) if review else "",
               review.note if review else "", closed.actor.display_name if closed else "",
               local_timestamp(closed.created_at) if closed else "", closed.note if closed else "", item.previous_id or "",
               item.province, item.city, item.get_tutor_signature_display() if item.tutor_signature else "历史记录未填写"]
        yield writer.writerow([csv_text(value) for value in row])
