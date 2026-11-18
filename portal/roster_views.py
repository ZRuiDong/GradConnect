from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_safe
from .forms import StudentRosterUploadForm
from .models import StudentRosterBatch
from .roster_imports import HEADERS, confirm_roster, parse_roster
from .views import page_context, staff_only


@staff_only
def roster_upload(request, kind):
    if kind not in HEADERS:
        raise Http404
    form = StudentRosterUploadForm(request.POST if request.method == "POST" else None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            rows = parse_roster(form.cleaned_data["file"], kind)
        except ValidationError as exc:
            form.add_error("file", exc)
        else:
            batch = StudentRosterBatch.objects.create(kind=kind, uploaded_by=request.user, row_count=len(rows), payload=rows)
            return redirect("roster_preview", pk=batch.pk)
    return render(request, "portal/roster_upload.html", {"form": form, "kind": kind,
        "title": StudentRosterBatch.Kind(kind).label})


@staff_only
@require_safe
def roster_template(request, kind):
    if kind not in HEADERS:
        raise Http404
    file = Path(settings.BASE_DIR) / "portal" / "import_templates" / f"student-{kind}.xlsx"
    return FileResponse(file.open("rb"), as_attachment=True, filename=f"student-{kind}-template.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@staff_only
def roster_preview(request, pk):
    batch = get_object_or_404(StudentRosterBatch, pk=pk, uploaded_by=request.user)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "confirm":
            try:
                changed = confirm_roster(batch.pk, request.user)
            except ValidationError as exc:
                messages.error(request, "；".join(exc.messages))
                if not StudentRosterBatch.objects.filter(pk=pk, uploaded_by=request.user).exists():
                    return redirect('account_list')
            except (IntegrityError, OperationalError):
                messages.error(request, "数据正在被其他操作修改，本次未保存，请重新上传并核对。")
            else:
                messages.success(request, f"已将{changed}名学生设为毕业，登录状态和业务记录保持不变。" if batch.kind == "graduate" else f"已保存{changed}条履职记录，当前职务按任职日期自动显示。")
                return redirect("account_list")
        elif action == "cancel":
            with transaction.atomic():
                batch = get_object_or_404(StudentRosterBatch.objects.select_for_update(), pk=pk, uploaded_by=request.user)
                if batch.is_confirmed:
                    messages.error(request, "已处理的批次不能取消。")
                else:
                    batch.delete()
                    messages.info(request, "已取消，本次名单未修改任何学生资料。")
                    return redirect("account_list")
        else:
            messages.error(request, "请选择确认或取消。")
        return redirect("roster_preview", pk=pk)
    return render(request, "portal/roster_preview.html", {**page_context(request, batch.payload, per_page=100), "batch": batch,
        "expired": batch.created_at < timezone.now() - timedelta(days=1)})
