from django.http import JsonResponse
from django.views.decorators.http import require_POST, require_GET
from .analytics import touch_activity
from .models import User
from .views import staff_only, student_only


@student_only
@require_POST
def activity_ping(request):
    touch_activity(request.user)
    return JsonResponse({"ok": True})


@staff_only
@require_GET
def account_presence(request):
    values = request.GET.getlist("ids")
    try:
        if not values or len(values) > 100:
            raise ValueError
        ids = {int(value) for value in values}
        if any(value <= 0 or value >= 2**63 for value in ids):
            raise ValueError
    except (ValueError, TypeError):
        return JsonResponse({"error": "学生编号无效，最多查询100名学生。"}, status=400)
    students = User.objects.filter(pk__in=ids, is_staff=False, is_superuser=False).only(
        "pk", "is_active", "is_archived", "enrollment_status", "online_until")
    return JsonResponse({"statuses": {str(student.pk): student.presence for student in students}})
