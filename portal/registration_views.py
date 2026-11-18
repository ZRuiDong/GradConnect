from datetime import date, datetime, time
from pathlib import Path
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Q
from django.forms.models import model_to_dict
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe
from .files import send_private_file
from .forms import RegistrationFilterForm, RegistrationForm, RegistrationReviewForm
from .models import Registration, RegistrationEvent
from .registration_services import save_registration, withdraw_registration
from .services import audit
from .uploads import REGISTRATION_FILE_TYPES
from .views import form_page, page_context, staff_only, student_only


def module_context(kind):
    prefix = {"internship": "internship", "rental": "rental"}.get(kind)
    if not prefix:
        raise Http404
    return {"kind": kind, "module_title": Registration.Kind(kind).label,
            **{f"{action}_name": f"{prefix}_{action}" for action in ["list", "create", "detail", "review", "file", "withdraw", "resubmit"]}}


@login_required
def applications_home(request):
    return redirect("leave_list")


def owned_registration(request, kind, pk):
    qs = Registration.objects.select_related("student").filter(kind=kind)
    if not request.user.is_staff:
        qs = qs.filter(student=request.user)
    return get_object_or_404(qs, pk=pk)


@login_required
def registration_list(request, kind):
    context = module_context(kind)
    form = RegistrationFilterForm(request.GET)
    qs = Registration.objects.select_related("student").filter(kind=kind)
    if not request.user.is_staff:
        qs = qs.filter(student=request.user)
    if form.is_valid():
        data = form.cleaned_data
        if data["q"]:
            qs = qs.filter(Q(student_name__icontains=data["q"]) | Q(student_number__icontains=data["q"]) |
                           Q(employer__icontains=data["q"]) | Q(address__icontains=data["q"]))
        if data["status"]:
            qs = qs.filter(status=data["status"])
        if data["start_date"] and data["start_date"] > date.min:
            qs = qs.filter(ends_at__gte=timezone.make_aware(datetime.combine(data["start_date"], time.min)))
        if data["end_date"]:
            qs = qs.filter(starts_at__lte=timezone.make_aware(datetime.combine(data["end_date"], time.max)))
    else:
        qs = qs.none()
    return render(request, "portal/registration_list.html", {**context, **page_context(request, qs), "filter_form": form})


@student_only
def registration_create(request, kind, pk=None):
    context = module_context(kind)
    source, initial = None, {}
    if pk is not None:
        source = get_object_or_404(Registration, pk=pk, student=request.user, kind=kind)
        replacement = Registration.objects.filter(previous=source).first()
        if replacement:
            messages.info(request, '原申请已经重新提交，以下为新申请。')
            return redirect(context['detail_name'], pk=replacement.pk)
        if source.status not in {'withdrawn', 'rejected'}:
            messages.error(request, '请先撤回待审核申请；已通过的申请不能修改。')
            return redirect(context['detail_name'], pk=pk)
        initial = model_to_dict(source, fields=RegistrationForm.Meta.fields)
        initial['has_dorm'] = 'yes' if source.has_dorm is True else 'no' if source.has_dorm is False else ''
    form = RegistrationForm(request.POST if request.method == 'POST' else None, request.FILES or None,
        user=request.user, kind=kind, source=source, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            item = save_registration(form, request.user, kind, source)
        except ValidationError as exc:
            form.add_error(None, exc)
        except (IntegrityError, OperationalError):
            form.add_error(None, '申请正在被其他操作修改，本次未提交，请返回详情核对。')
        else:
            messages.success(request, "申请已提交，请等待管理员审核。")
            return redirect(context["detail_name"], pk=item.pk)
    return form_page(request, form, f"修改并重新提交{context['module_title']}" if source else f"提交{context['module_title']}",
        reverse(context["detail_name"], args=[source.pk]) if source else reverse(context["list_name"]),
        "原申请和审核历史保留。可保留原附件，也可替换后重新提交。" if source else
        "请完整填写信息并上传申请附件。可去办事指南（业务流程）模块寻找申请模板。", "重新提交申请" if source else "提交申请")


@student_only
@require_POST
def registration_withdraw(request, kind, pk):
    context = module_context(kind)
    owned_registration(request, kind, pk)
    if request.POST.get('confirmed') != 'yes':
        messages.error(request, '请先确认撤回申请。')
        return redirect(context['detail_name'], pk=pk)
    try:
        withdraw_registration(pk, request.user, kind)
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
        return redirect(context['detail_name'], pk=pk)
    messages.success(request, '申请已撤回，原记录保留；修改后可重新提交。')
    return redirect(context['resubmit_name'], pk=pk)


@login_required
def registration_detail(request, kind, pk):
    context = module_context(kind)
    item = owned_registration(request, kind, pk)
    from .inbox import topic_context
    return render(request, "portal/registration_detail.html", {**context, "item": item,
        **topic_context(request.user, kind, item.pk),
        "events": item.events.select_related("actor"), "attachments": item.attachments.all(), "review_form": RegistrationReviewForm(),
        "replacement": Registration.objects.filter(previous=item).first()})


@staff_only
@require_POST
def registration_review(request, kind, pk):
    context = module_context(kind)
    item = owned_registration(request, kind, pk)
    form = RegistrationReviewForm(request.POST)
    if not form.is_valid():
        return render(request, "portal/registration_detail.html", {**context, "item": item,
            "events": item.events.select_related("actor"), "attachments": item.attachments.all(), "review_form": form}, status=400)
    target = "approved" if form.cleaned_data["action"] == "approve" else "rejected"
    with transaction.atomic():
        changed = Registration.objects.filter(pk=item.pk, status="pending").update(status=target, updated_at=timezone.now())
        if not changed:
            messages.error(request, "申请状态已经变化，请刷新后查看。")
        else:
            RegistrationEvent.objects.create(registration=item, actor=request.user, status=target, note=form.cleaned_data["note"])
            from .inbox import deliver
            deliver(kind, item, request.user, f'{kind}:{item.pk}:{target}',
                f'{context["module_title"]} #{item.pk}：{Registration.Status(target).label}')
            audit(request.user, f"{context['module_title']}：{form.cleaned_data['action']}", item)
            messages.success(request, "审核结果已保存。")
    return redirect(context["detail_name"], pk=pk)


@login_required
@require_safe
def registration_file(request, kind, registration_pk, pk):
    module_context(kind)
    item = owned_registration(request, kind, registration_pk)
    attachment = get_object_or_404(item.attachments, pk=pk)
    content_type = REGISTRATION_FILE_TYPES.get(Path(attachment.file.name).suffix.lower())
    if not content_type:
        raise Http404
    return send_private_file(request, attachment.file, content_type, attachment.original_name, download=True)
