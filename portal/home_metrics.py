from datetime import datetime, time
from django.db.models import Count, Exists, OuterRef, Q, Sum
from django.db.models.functions import TruncMonth
from django.urls import reverse
from django.utils import timezone
from .models import ContentVisitDaily, Conversation, InquiryCategory, Leave, Message, MonthlyAttendance, Notification, Policy, Registration, ServiceGuide, User, Video


COLORS = ["#28796a", "#699fba", "#dba560", "#839b62", "#ae88ad", "#5d87b0", "#cf8679", "#a5b4ac"]


def distribution(queryset, choices):
    counts = dict(queryset.order_by().values_list("category").annotate(n=Count("pk")))
    total = sum(counts.values())
    rows, stops, angle = [], [], 0
    for index, (key, label) in enumerate(choices):
        count = counts.get(key, 0)
        percentage = count * 100 / total if total else 0
        end = angle + percentage * 3.6
        color = COLORS[index % len(COLORS)]
        if count:
            stops.append(f"{color} {angle:.3f}deg {end:.3f}deg")
        rows.append({"key": key, "label": label, "count": count, "percentage": f"{percentage:.1f}", "width": f"{percentage:.4f}", "color": color})
        angle = end
    return {"rows": rows, "total": total, "gradient": "conic-gradient("+", ".join(stops)+")" if total else "#edf1ed"}


def home_context(user):
    staff, today = user.is_staff, timezone.localdate()
    consultations = Conversation.objects.all() if staff else Conversation.objects.filter(student=user)
    replies = Message.objects.filter(conversation_id=OuterRef("pk"), author__is_staff=True)
    consultations = consultations.annotate(_answered=Exists(replies))
    total = consultations.aggregate(total=Count("pk"), answered=Count("pk", filter=Q(_answered=True)), waiting=Count("pk", filter=Q(awaiting_reply=True)))
    rate = total["answered"] * 100 / total["total"] if total["total"] else 0
    from .analytics import aggregate_metrics
    click_scope = ContentVisitDaily.objects.all() if staff else user.content_visits.all()
    click_metrics = aggregate_metrics(click_scope, today)
    by_kind = click_scope.filter(day__lte=today).order_by().values('kind').annotate(total=Sum('views'),
        month=Sum('views', filter=Q(day__gte=today.replace(day=1))), today=Sum('views', filter=Q(day=today)))
    kind_metrics = {row['kind']: row for row in by_kind}
    click_breakdown = [{'key': key, 'label': label, 'url': reverse(key+'_list'),
        **{period: kind_metrics.get(key, {}).get(period) or 0 for period in ['total', 'month', 'today']}}
        for key, label in ContentVisitDaily.Kind.choices]
    notifications = Notification.objects.filter(status="published")
    policies = Policy.objects.filter(status="published", published_revision__isnull=False)
    leaves = Leave.objects.all() if staff else Leave.objects.filter(student=user)
    registrations = Registration.objects.all() if staff else Registration.objects.filter(student=user)
    leave_counts = leaves.aggregate(pending=Count("pk", filter=Q(status="pending")), approved=Count("pk", filter=Q(status="approved")))
    pending_regs = dict(registrations.filter(status="pending").order_by().values_list("kind").annotate(n=Count("pk")))
    month_number = today.year*12+today.month-1
    months = [(month_number-offset)//12*100+(month_number-offset)%12+1 for offset in range(5,-1,-1)]
    start = timezone.make_aware(datetime.combine(today.replace(year=months[0]//100, month=months[0]%100, day=1), time.min))
    trend_rows = consultations.filter(created_at__gte=start).order_by().annotate(month=TruncMonth("created_at", tzinfo=timezone.get_current_timezone())).values("month").annotate(total=Count("pk"), answered=Count("pk", filter=Q(_answered=True)))
    monthly = {row["month"].year*100+row["month"].month: row for row in trend_rows}
    maximum = max([monthly.get(month, {}).get("total", 0) for month in months] or [1]) or 1
    trend = []
    for month in months:
        row = monthly.get(month, {})
        amount, answered = row.get("total", 0), row.get("answered", 0)
        trend.append({"label": f"{month//100}年{month%100}月", "short_label": f"{month%100}月", "total": amount, "answered": answered,
            "height": f"{amount/maximum*100:.4f}", "answered_height": f"{answered/maximum*100:.4f}"})
    notification_count, policy_count = notifications.count(), policies.count()
    kpis = [
        {"label": "累计咨询" if staff else "我的咨询", "value": total["total"], "hint": "按咨询会话统计", "url": reverse("consultation_list"), "tone": "green", "symbol": "◌"},
        {"label": "回复率", "value": f"{rate:.1f}%" if total["total"] else "—", "hint": "至少获一次教师回复" if total["total"] else "暂无可统计咨询", "url": reverse("consultation_list"), "tone": "blue", "symbol": "↗"},
        {"label": "待回复咨询", "value": total["waiting"], "hint": "包含学生继续追问", "url": reverse("consultation_list")+"?waiting=1", "tone": "amber", "symbol": "◷"},
        {"label": "已发布通知", "value": notification_count, "hint": "覆盖七类校园信息", "url": reverse("notification_list"), "tone": "coral", "symbol": "◎"},
        {"label": "有效规章制度", "value": policy_count, "hint": "已发布且具有有效版本", "url": reverse("policy_list"), "tone": "blue", "symbol": "▤"},
    ]
    if staff:
        students = User.objects.filter(is_staff=False, is_superuser=False, is_archived=False, enrollment_status="enrolled").count()
        kpis.append({"label": "在校学生", "value": students, "hint": "不含已归档及已毕业学生", "url": reverse("account_list")+"?enrollment_status=enrolled", "tone": "green", "symbol": "◈"})
    else:
        attendance = MonthlyAttendance.objects.filter(student=user, month=today.replace(day=1)).first()
        kpis.append({"label": "本月打卡总天数", "value": attendance.total_days if attendance else "—", "hint": "打卡天数＋请假天数" if attendance else "尚未导入本月记录", "url": reverse("attendance_list"), "tone": "green", "symbol": "▦"})
    tasks = [{"label": "待审核请假", "count": leave_counts["pending"], "url": reverse("leave_list")+"?status=pending"},
        {"label": "待销假", "count": leave_counts["approved"], "url": reverse("leave_list")+"?status=approved"},
        {"label": "待审核实习", "count": pending_regs.get("internship", 0), "url": reverse("internship_list")+"?status=pending"},
        {"label": "待审核租房", "count": pending_regs.get("rental", 0), "url": reverse("rental_list")+"?status=pending"}]
    from .student_services import current_tenures
    student_roles = list(current_tenures().filter(student=user).order_by('-starts_on', '-pk')) if not staff else []
    return {"today": today, "home_kpis": kpis, "consultation_cards": kpis[:3], "inventory_cards": kpis[3:5],
        "student_summary_card": kpis[-1], "home_tasks": tasks, "student_roles": student_roles,
        "click_metrics": click_metrics, "click_breakdown": click_breakdown, "consultation_total": total["total"], "consultation_answered": total["answered"],
        "reply_rate": f"{rate:.1f}", "consultation_distribution": distribution(consultations, InquiryCategory.choices),
        "notification_distribution": distribution(notifications, Notification.Category.choices), "consultation_trend": trend, "trend_maximum": maximum,
        "pending_count": leave_counts["pending"], "approved_count": leave_counts["approved"], "consult_count": total["waiting"], "policy_count": policy_count,
        "recent_leaves": leaves.select_related("student")[:4], "recent_consultations": consultations.select_related("student")[:4],
        "recent_notifications": notifications[:4], "recent_policies": policies.select_related("published_revision")[:4],
        "recent_guides": ServiceGuide.objects.filter(status="published")[:3]}
