import secrets
from datetime import timedelta
from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from .forms import AccountForm, CadreTenureForm, CadreTenureFormSet, StudentCreationForm, StudentDeleteConfirmForm, StudentFilterForm
from .models import CadreTenure, StudentImportBatch, StudentRosterBatch, User
from .services import audit
from .student_services import delete_students, filtered_students, protected_student_ids, student_identity_digest, student_queryset
from .student_state import student_edit_digest
from .views import page_context, staff_only


STUDENT_TABLE_COLUMNS = [
    {"key": "select", "label": "选择", "width": 46, "min": 46, "max": 46},
    {"key": "identity", "label": "学号 / 姓名", "width": 190, "min": 120, "max": 480},
    {"key": "id_number", "label": "身份证号", "width": 164, "min": 130, "max": 300},
    {"key": "enrollment", "label": "在校状态", "width": 88, "min": 76, "max": 160},
    {"key": "branch", "label": "党支部 / 班级", "width": 184, "min": 110, "max": 450},
    {"key": "politics", "label": "政治面貌", "width": 168, "min": 100, "max": 450},
    {"key": "roles", "label": "当前履职职务", "width": 200, "min": 120, "max": 500},
    {"key": "active", "label": "账号状态", "width": 88, "min": 76, "max": 180},
    {"key": "actions", "label": "操作", "width": 300, "min": 280, "max": 420},
]


def filter_form(request):
    params = request.GET.copy()
    params.setdefault("archive", "current")
    return StudentFilterForm(params)


def filter_presentation(request, form):
    chips = []
    valid = form.is_valid()
    data = form.cleaned_data

    def removal_url(keys, value=None):
        params = request.GET.copy()
        params.pop("page", None)
        for key in keys:
            remaining = [item for item in params.getlist(key) if item != value] if value is not None else []
            if remaining:
                params.setlist(key, remaining)
            else:
                params.pop(key, None)
        return reverse("account_list") + ("?" + params.urlencode() if params else "")

    if valid:
        if data["q"]:
            chips.append({"label": f"搜索：{data['q']}", "url": removal_url(["q"])})
        for name in ["student_class", "party_branch", "political_status", "current_position"]:
            labels = dict(form.fields[name].choices)
            for value in data[name]:
                chips.append({"label": labels[value], "url": removal_url([name], value)})
        for name in ["enrollment_status", "cadre", "is_active", "completeness", "archive"]:
            if data[name] and not (name == "archive" and data[name] == "current"):
                chips.append({"label": dict(form.fields[name].choices)[data[name]], "url": removal_url([name])})
    advanced = ["enrollment_status", "cadre", "is_active", "completeness", "archive"]
    count = sum(bool(data.get(name)) for name in ["enrollment_status", "cadre", "is_active", "completeness"])
    count += int(data.get("archive", "current") != "current" and bool(data.get("archive")))
    return {"filter_chips": chips, "advanced_filter_count": count,
            "advanced_filters_open": bool(count or any(name in form.errors for name in advanced) or form.non_field_errors())}


@staff_only
def account_list(request):
    form = filter_form(request)
    qs = filtered_students(form)
    pagination = page_context(request, qs)
    return render(request, "portal/account_list.html", {**pagination, **filter_presentation(request, form), "filter_form": form,
        "table_columns": STUDENT_TABLE_COLUMNS, "table_width": sum(column["width"] for column in STUDENT_TABLE_COLUMNS),
        "pending_imports": StudentImportBatch.objects.filter(uploaded_by=request.user, is_confirmed=False,
            created_at__gte=timezone.now() - timedelta(days=1))[:5],
        "pending_rosters": StudentRosterBatch.objects.filter(uploaded_by=request.user, is_confirmed=False,
            created_at__gte=timezone.now() - timedelta(days=1))[:5]})


@staff_only
def account_detail(request, pk):
    item = get_object_or_404(student_queryset(), pk=pk)
    context = page_context(request, item.cadre_tenures.all())
    token = signing.dumps({"student": item.pk, "actor": request.user.pk, "digest": student_edit_digest(item)}, salt="student-edit", compress=True)
    from .analytics import student_activity_context
    return render(request, "portal/account_detail.html", {**context, **student_activity_context(request, item), "item": item, "tenures": context["page_obj"], "edit_token": token})


def edit_student(request, pk=None):
    with transaction.atomic():
        item = get_object_or_404(User.objects.select_for_update().filter(is_staff=False, is_superuser=False), pk=pk) if pk else User()
        was_active = item.is_active
        current_digest = student_edit_digest(item) if pk else None
        edit_conflict = False
        edit_token = request.POST.get("edit_token", "") if request.method == "POST" else (
            signing.dumps({"student": item.pk, "actor": request.user.pk, "digest": current_digest}, salt="student-edit", compress=True) if pk else "")
        form_class = AccountForm if pk else StudentCreationForm
        form = form_class(request.POST or None, instance=item)
        tenure_data = request.POST if request.method == "POST" and any(key.startswith("tenures-") for key in request.POST) else None
        formset = CadreTenureFormSet(tenure_data, instance=item, prefix="tenures")
        if request.method == "POST":
            profile_valid = form.is_valid()
            tenure_valid = not formset.is_bound or formset.is_valid()
            if pk:
                try:
                    saved = signing.loads(edit_token, salt="student-edit", max_age=settings.SESSION_COOKIE_AGE)
                    edit_conflict = not isinstance(saved, dict) or saved.get("student") != item.pk or saved.get("actor") != request.user.pk or saved.get("digest") != current_digest
                except (signing.BadSignature, ValueError, TypeError):
                    edit_conflict = True
                if edit_conflict:
                    form.add_error(None, "资料或干部履职记录已变化，或编辑页面已过期。本次未保存，请重新加载最新资料并核对。")
            if profile_valid and tenure_valid and not edit_conflict:
                item = form.save(commit=False)
                if pk and was_active and not item.is_active:
                    item.session_revision += 1
                item.save()
                if formset.is_bound:
                    formset.instance = item
                    formset.save()
                audit(request.user, "更新学生信息及履职历史" if pk else "创建学生账号", item)
                messages.success(request, "学生信息已保存。" if pk else "学生账号及资料已创建，请线下告知学生初始密码；首次登录须设置自己的新密码。")
                return redirect("account_detail", pk=item.pk)
    return render(request, "portal/account_form.html", {"form": form, "tenure_formset": formset, "item": item,
        "title": "编辑学生信息" if pk else "新增学生", "is_new": pk is None, "edit_token": edit_token,
        "edit_conflict": edit_conflict}, status=409 if edit_conflict else 200)


@staff_only
def account_create(request):
    return edit_student(request)


@staff_only
def account_edit(request, pk):
    return edit_student(request, pk)


@staff_only
@require_POST
def account_tenure_action(request, pk):
    action = request.POST.get("action")
    if action not in {"save", "delete"} or request.POST.get("confirmed") != "yes":
        return JsonResponse({"message": "请先确认本次保存或删除操作。"}, status=400)
    try:
        tenure_id = int(request.POST["tenure_id"]) if request.POST.get("tenure_id") else None
        if tenure_id is not None and not 0 < tenure_id < 2**63:
            raise ValueError
    except (TypeError, ValueError):
        raise Http404
    with transaction.atomic():
        student = get_object_or_404(User.objects.select_for_update(), pk=pk, is_staff=False, is_superuser=False)
        try:
            token = signing.loads(request.POST.get("edit_token", ""), salt="student-edit", max_age=settings.SESSION_COOKIE_AGE)
            valid = (isinstance(token, dict) and token.get("student") == student.pk and
                token.get("actor") == request.user.pk and token.get("digest") == student_edit_digest(student))
        except (signing.BadSignature, ValueError, TypeError):
            valid = False
        if not valid:
            return JsonResponse({"message": "学生资料或履职记录已变化，或页面已过期。请重新加载并核对后操作。",
                                 "reload_url": reverse("account_edit", args=[pk])}, status=409)
        item = get_object_or_404(CadreTenure.objects.select_for_update(), pk=tenure_id, student=student) if tenure_id else None
        if action == "delete":
            if item is None:
                return JsonResponse({"message": "请选择要删除的履职记录。"}, status=400)
            audit(request.user, "删除干部履职记录", item)
            item.delete()
            saved_id = None
        else:
            form = CadreTenureForm(request.POST, instance=item or CadreTenure(student=student))
            if not form.is_valid():
                return JsonResponse({"message": "请修正履职记录中标出的内容。", "errors": form.errors.get_json_data()}, status=400)
            if item is None and student.cadre_tenures.count() >= 100:
                return JsonResponse({"message": "每位学生最多维护100条履职记录。"}, status=400)
            item = form.save()
            audit(request.user, "更新干部履职记录" if tenure_id else "新增干部履职记录", item)
            saved_id = item.pk
        edit_token = signing.dumps({"student": student.pk, "actor": request.user.pk,
            "digest": student_edit_digest(student)}, salt="student-edit", compress=True)
        html = render_to_string("partials/tenure_rows.html", {"tenure_formset": CadreTenureFormSet(instance=student, prefix="tenures")}, request=request)
        return JsonResponse({"message": "履职记录已删除。" if action == "delete" else "履职记录已保存。",
                             "html": html, "edit_token": edit_token, "saved_id": saved_id})


@staff_only
@require_POST
def account_status(request, pk, action):
    with transaction.atomic():
        item = get_object_or_404(User.objects.select_for_update(), pk=pk, is_staff=False, is_superuser=False)
        if action == "restore" and item.is_archived:
            item.is_archived = False
            item.is_active = False
        elif action == "disable":
            if item.is_active:
                item.session_revision += 1
            item.is_active = False
        elif action == "enable" and not item.is_archived:
            item.is_active = True
        else:
            raise Http404
        item.online_until = None
        item.save(update_fields=["is_active", "is_archived", "session_revision", "online_until"])
        audit(request.user, f"学生账号：{action}", item)
    messages.success(request, "已恢复到学生信息列表，登录仍保持停用，可核对后单独启用。" if action == "restore" else "登录状态已更新。")
    return redirect("account_detail", pk=pk)


@staff_only
@require_POST
def account_delete_prepare(request):
    form = filter_form(request)
    qs = filtered_students(form).filter(is_archived=False)
    ids = request.POST.getlist("ids")
    if request.POST.get("single_id"):
        ids = [request.POST["single_id"]]
    elif request.POST.get("selection_mode") == "all":
        ids = list(qs.values_list("pk", flat=True))
    try:
        ids = sorted({int(value) for value in ids})
        if any(value <= 0 or value >= 2**63 for value in ids):
            raise ValueError
    except (ValueError, TypeError):
        raise Http404
    students = list(qs.filter(pk__in=ids))
    if not form.is_valid() or not ids or len(students) != len(ids):
        messages.error(request, "请使用有效筛选条件选择当前学生，已归档学生和管理员不能删除。")
        return redirect(reverse("account_list") + ("?" + request.GET.urlencode() if request.GET else ""))
    token = secrets.token_urlsafe(24)
    now = timezone.now().timestamp()
    previews = {key: value for key, value in request.session.get("student_delete_previews", {}).items()
                if now - value["created"] < 1800}
    if len(previews) >= 5:
        previews.pop(next(iter(previews)))
    previews[token] = {"ids": ids, "digest": student_identity_digest(students), "created": now, "owner": request.user.pk}
    request.session["student_delete_previews"] = previews
    return redirect("account_delete_confirm", token=token)


@staff_only
def account_delete_confirm(request, token):
    previews = request.session.get("student_delete_previews", {})
    intent = previews.get(token)
    if not intent or intent["owner"] != request.user.pk or timezone.now().timestamp() - intent["created"] >= 1800:
        raise Http404("删除核对已失效，请重新选择学生。")
    form = StudentDeleteConfirmForm(request.POST or None)
    if request.method == "POST":
        if request.POST.get("action") == "cancel":
            previews.pop(token)
            request.session["student_delete_previews"] = previews
            return redirect("account_list")
        if form.is_valid():
            try:
                deleted, archived = delete_students(intent["ids"], request.user, intent["digest"])
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                previews.pop(token)
                request.session["student_delete_previews"] = previews
                messages.success(request, f"已彻底删除 {deleted} 名无业务关联的学生，归档并停用 {archived} 名有历史记录的学生。")
                return redirect("account_list")
    students = list(student_queryset().filter(pk__in=intent["ids"]))
    protected = protected_student_ids(intent["ids"])
    for item in students:
        item.will_archive = item.pk in protected
    return render(request, "portal/account_delete_confirm.html", {**page_context(request, students, per_page=100),
        "form": form, "total_count": len(students), "archive_count": len(protected), "delete_count": len(students) - len(protected)},
        status=400 if form.errors else 200)
