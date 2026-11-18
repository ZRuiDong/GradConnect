from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponseBadRequest
from django.views.decorators.http import require_GET
from .attendance_queries import filtered_attendance
from .reports import export_attendance, export_students
from .sorting import sort_for_page
from .student_services import filtered_students
from .student_views import filter_form
from .views import staff_only


@login_required
@require_GET
def attendance_export(request):
    form, records, _, _, _ = filtered_attendance(request)
    if not form.is_valid():
        return HttpResponseBadRequest('筛选或排序条件无效，请返回月度打卡页面修正后导出。')
    try:
        return export_attendance(request, records, form)
    except ValidationError as exc:
        return HttpResponseBadRequest(' '.join(exc.messages))


@staff_only
@require_GET
def account_export(request):
    form = filter_form(request)
    records, sorting = sort_for_page(request, filtered_students(form), 'account_list')
    if not form.is_valid() or sorting.get('table_sort_error'):
        return HttpResponseBadRequest('筛选或排序条件无效，请返回学生信息汇总页面修正后导出。')
    try:
        return export_students(request, records, form)
    except ValidationError as exc:
        return HttpResponseBadRequest(' '.join(exc.messages))
