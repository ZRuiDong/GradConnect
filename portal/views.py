import csv
import hashlib
import json
from datetime import date, timedelta
from functools import wraps
from pathlib import Path
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import Count, F, Max, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce, NullIf
from django.forms.models import model_to_dict
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from urllib.parse import urlsplit, urlunsplit
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe
from django.views.decorators.clickjacking import xframe_options_sameorigin
from .files import send_private_file
from .pagination import paginated_context
from .sorting import sort_for_page
from .content_order import CONTENT_MODULES, content_page, move_content
from .forms import (ATTENDANCE_SORT_CHOICES, AttendanceMonthForm, AttendanceFilterForm, AttendanceUploadForm, ConsultationFilterForm, ConversationForm, FAQFilterForm, FAQForm, LeaveFilterForm, LeaveForm,
                    LoginForm, MessageForm, NotificationFilterForm, NotificationForm,
                    PolicyFilterForm, PolicyForm, StudentImportForm,
                    STUDENT_IMPORT_HEADERS, STUDENT_IMPORT_COLUMNS, STUDENT_POLITICS_UPDATE_HEADERS,
                    ServiceGuideForm, ServiceGuideFilterForm, VideoFilterForm, VideoForm, VideoMetadataForm)
from .models import (AttendanceBatch, Audit, Conversation, FAQ, InquiryCategory, Leave, LeaveAttachment, LeaveEvent, MonthlyAttendance,
                     LoginGuard, Message, Notification, Policy, PolicyRevision,
                     Publication, ServiceGuide, GuideAttachment, StudentImportBatch, User, Video)
from .services import (audit, confirm_attendance, confirm_student_accounts, parse_attendance,
                       prepare_student_account_import, transition_leave)
from .leave_reports import filtered_leaves, leave_csv_rows
from .uploads import GUIDE_FILE_TYPES, NOTIFICATION_FILE_TYPES, rollback_uploads


def staff_only(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped


def student_only(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if request.user.is_staff:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped


def page_context(request, queryset, per_page=12, params=None):
    queryset, sorting = sort_for_page(request, queryset)
    return {**paginated_context(request, queryset, per_page, params), **sorting}


def form_page(request, form, title, back, hint="", button="保存", **extra):
    return render(request, "portal/form.html", {"form": form, "title": title, "back": back,
                  "hint": hint, "button": button, **extra})


def category_tabs(request, choices, list_name, all_label):
    selected = request.GET.get("category", "")
    tabs = []
    for value, label in [("", all_label), *choices]:
        params = request.GET.copy()
        params.pop("page", None)
        params["category"] = value
        tabs.append({"label": label, "active": selected == value,
                     "url": reverse(list_name) + "?" + params.urlencode()})
    return tabs


def query_id(request, key):
    value = request.GET.get(key)
    if value is None or value == "":
        return None
    try:
        result = int(value)
        if not 0 < result < 2**63:
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise Http404


@staff_only
@require_POST
def content_order_action(request, kind, pk):
    if kind not in CONTENT_MODULES:
        raise Http404
    target = request.POST.get("return_to", "")
    if not target or not url_has_allowed_host_and_scheme(target, {request.get_host()}, require_https=request.is_secure()):
        target = reverse(CONTENT_MODULES[kind][1])
    try:
        page = move_content(request, kind, pk)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        from django.http import QueryDict
        parts = urlsplit(target)
        query = QueryDict(parts.query, mutable=True)
        query["page"] = str(page)
        target = urlunsplit((parts.scheme, parts.netloc, parts.path, query.urlencode(), f"resource-item-{pk}"))
        messages.success(request, "展示顺序已更新，学生端同步生效。")
    return redirect(target)


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = LoginForm(request, data=request.POST if request.method == "POST" else None)
    if request.method == "POST":
        key = hashlib.sha256(request.POST.get("username", "").strip().casefold().encode()).hexdigest()
        now = timezone.now()
        guard = LoginGuard.objects.filter(key=key).first()
        if guard and guard.failures >= 5 and guard.updated_at > now - timedelta(minutes=15):
            form.add_error(None, "登录失败次数过多，请 15 分钟后重试，或线下联系管理员。")
        elif form.is_valid():
            LoginGuard.objects.filter(key=key).delete()
            login(request, form.get_user())
            return redirect("dashboard")
        else:
            with transaction.atomic():
                guard, _ = LoginGuard.objects.get_or_create(key=key, defaults={"updated_at": now})
                if guard.updated_at < now - timedelta(minutes=15):
                    LoginGuard.objects.filter(pk=guard.pk).update(failures=1, updated_at=now)
                else:
                    LoginGuard.objects.filter(pk=guard.pk).update(failures=F("failures") + 1, updated_at=now)
    return render(request, "registration/login.html", {"form": form})


@require_safe
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


@login_required
def dashboard(request):
    from .home_metrics import home_context
    return render(request, "portal/dashboard.html", home_context(request.user))


@login_required
def notification_list(request):
    form = NotificationFilterForm(request.GET, user=request.user)
    qs = Notification.objects.select_related("created_by")
    if not request.user.is_staff:
        qs = qs.filter(status=Notification.Status.PUBLISHED)
    if form.is_valid():
        data = form.cleaned_data
        if data["category"]:
            qs = qs.filter(category=data["category"])
        if data["q"]:
            qs = qs.filter(Q(title__icontains=data["q"]) | Q(summary__icontains=data["q"]) | Q(body__icontains=data["q"]))
        if data.get("status"):
            qs = qs.filter(status=data["status"])
    else:
        qs = qs.none()
    selected = request.GET.get("category", "")
    pagination = content_page(request, qs, page_context(request, qs), "notification")
    return render(request, "portal/notification_list.html", {**pagination, "filter_form": form,
        "category_tabs": category_tabs(request, Notification.Category.choices, "notification_list", "全部通知"),
        "selected_category": selected if selected in Notification.Category.values else ""})


def visible_notification(request, pk):
    qs = Notification.objects.select_related("created_by", "updated_by")
    if not request.user.is_staff:
        qs = qs.filter(status=Notification.Status.PUBLISHED)
    return get_object_or_404(qs, pk=pk)


@login_required
def notification_detail(request, pk):
    item = visible_notification(request, pk)
    extension = Path(item.attachment.name).suffix.lower() if item.attachment else ""
    from .analytics import detail_metrics
    return render(request, "portal/notification_detail.html", {"item": item, **detail_metrics(request, "notification", item),
        "attachment_name": f"{item.title}{extension}", "attachment_extension": extension,
        "attachment_is_pdf": extension == ".pdf",
        "attachment_is_image": NOTIFICATION_FILE_TYPES.get(extension, "").startswith("image/")})


@staff_only
def notification_edit(request, pk=None):
    with rollback_uploads() as uploads, transaction.atomic():
        item = get_object_or_404(Notification.objects.select_for_update(), pk=pk) if pk else None
        category = request.GET.get("category")
        initial = {"category": category} if category in Notification.Category.values else {}
        form = NotificationForm(request.POST or None, request.FILES or None, instance=item, initial=initial)
        if request.method == "POST" and form.is_valid():
            item = form.save(commit=False)
            if not item.pk:
                item.created_by = request.user
                item.status = Notification.Status.PUBLISHED
                item.published_at = timezone.now()
            item.updated_by = request.user
            uploads.save(item)
            audit(request.user, "更新通知" if pk else "发布通知", item)
            messages.success(request, "通知已更新并保持撤下状态，可确认后重新发布。" if item.status == Notification.Status.WITHDRAWN
                             else "通知已更新。" if pk else "通知已发布，学生可在相应类别下检索查看。")
            return redirect("notification_detail", pk=item.pk)
    return form_page(request, form, "修改 / 替换通知" if item else "发布通知", reverse("notification_list"),
        "选择通知类别，填写正文或上传 Word、Excel、PDF、图片等附件（最多 20 MB）。已发布通知的修改立即生效；已撤下通知修改后仍保持撤下。",
        "保存修改" if item else "发布通知")


@staff_only
@require_POST
def notification_action(request, pk, action):
    if action not in {"publish", "withdraw"}:
        raise Http404
    target = Notification.Status.PUBLISHED if action == "publish" else Notification.Status.WITHDRAWN
    with transaction.atomic():
        item = get_object_or_404(Notification.objects.select_for_update(), pk=pk)
        if item.status == target:
            messages.info(request, "该通知已经处于所选状态。")
        else:
            item.status = target
            item.updated_by = request.user
            if target == Notification.Status.PUBLISHED:
                item.published_at = timezone.now()
            item.save()
            audit(request.user, "重新发布通知" if action == "publish" else "撤下通知", item)
            messages.success(request, "通知已重新发布。" if action == "publish" else "通知已撤下，学生无法继续查看或下载附件。")
    return redirect("notification_detail", pk=pk)


@login_required
@require_safe
@xframe_options_sameorigin
def notification_file(request, pk):
    item = visible_notification(request, pk)
    extension = Path(item.attachment.name).suffix.lower() if item.attachment else ""
    content_type = NOTIFICATION_FILE_TYPES.get(extension)
    if not content_type:
        raise Http404
    previewable = extension == ".pdf" or content_type.startswith("image/")
    return send_private_file(request, item.attachment, content_type, f"{item.title}{extension}",
                             download=not previewable or request.GET.get("download") == "1")


@login_required
def policy_list(request):
    form = PolicyFilterForm(request.GET)
    qs = Policy.objects.select_related("published_revision").order_by(*Policy._meta.ordering)
    if request.user.is_staff:
        revisions = PolicyRevision.objects.filter(policy_id=OuterRef("pk")).order_by("-number")
        qs = qs.prefetch_related("revisions").annotate(_display_revision_id=Coalesce(
            Subquery(revisions.filter(is_draft=True).values("pk")[:1]), F("published_revision_id"), Subquery(revisions.values("pk")[:1])))
    else:
        qs = qs.filter(status=Publication.PUBLISHED, published_revision__isnull=False).annotate(_display_revision_id=F("published_revision_id"))
    if form.is_valid():
        data = form.cleaned_data
        matching = PolicyRevision.objects.all()
        if data["category"]:
            matching = matching.filter(category=data["category"])
        if q := data["q"]:
            categories = [value for value, label in PolicyRevision.Category.choices if q.casefold() in label.casefold()]
            matching = matching.filter(Q(title__icontains=q) | Q(summary__icontains=q) | Q(body__icontains=q) | Q(category__in=categories))
        if data["category"] or data["q"]:
            qs = qs.filter(_display_revision_id__in=matching.order_by().values("pk"))
    else:
        qs = qs.none()
    context = content_page(request, qs, page_context(request, qs), "policy")
    for item in context["page_obj"]:
        item.display_revision = next((revision for revision in item.revisions.all() if revision.pk == item._display_revision_id), None) if request.user.is_staff else item.published_revision
    selected = request.GET.get("category", "")
    return render(request, "portal/policy_list.html", {**context, "filter_form": form,
        "category_tabs": category_tabs(request, PolicyRevision.Category.choices, "policy_list", "全部类别"),
        "selected_category": selected if selected in PolicyRevision.Category.values else ""})


def visible_policy(request, pk):
    qs = Policy.objects.select_related("published_revision")
    if not request.user.is_staff:
        qs = qs.filter(status=Publication.PUBLISHED)
    return get_object_or_404(qs, pk=pk)


@login_required
def policy_detail(request, pk):
    item = visible_policy(request, pk)
    revision = item.published_revision
    draft = None
    if request.user.is_staff:
        draft = item.revisions.filter(is_draft=True).first()
        revision = draft or revision or item.revisions.first()
        if version := query_id(request, "version"):
            revision = get_object_or_404(item.revisions, pk=version)
    if revision is None:
        raise Http404
    from .analytics import detail_metrics
    return render(request, "portal/policy_detail.html", {**detail_metrics(request, "policy", item), "item": item, "revision": revision, "draft": draft,
        "versions": item.revisions.all() if request.user.is_staff else []})


@staff_only
def policy_edit(request, pk=None):
    with rollback_uploads() as uploads, transaction.atomic():
        item = get_object_or_404(Policy.objects.select_for_update(), pk=pk) if pk else None
        revision = item.revisions.filter(is_draft=True).first() if item else None
        if revision is None:
            source = (item.published_revision or item.revisions.first()) if item else None
            values = {f: getattr(source, f) for f in PolicyForm.Meta.fields} if source else {}
            revision = PolicyRevision(**values)
            if item:
                revision.number = (item.revisions.aggregate(n=Max("number"))["n"] or 0) + 1
        category = request.GET.get("category", "")
        initial = {"category": category if category in PolicyRevision.Category.values else ""} if not item else {}
        form = PolicyForm(request.POST or None, request.FILES or None, instance=revision, initial=initial)
        if request.method == "POST" and form.is_valid():
            if not item:
                item = Policy.objects.create(created_by=request.user)
            revision = form.save(commit=False)
            revision.policy = item
            revision.is_draft = True
            uploads.save(revision)
            item.save(update_fields=["updated_at"])
            audit(request.user, "保存制度草稿", revision)
            messages.success(request, "草稿已保存。请预览并确认发布，学生暂时看不到本次修改。")
            return redirect("policy_detail", pk=item.pk)
    return form_page(request, form, "编辑制度草稿" if item else "新增规章制度", reverse("policy_list"),
                     "已发布版本在新草稿确认发布前继续有效。PDF 附件上限 20 MB。", "保存草稿并预览")


@staff_only
@require_POST
def policy_action(request, pk, action):
    with transaction.atomic():
        item = get_object_or_404(Policy.objects.select_for_update(), pk=pk)
        if action == "publish":
            draft = item.revisions.filter(is_draft=True).first()
            if not draft:
                messages.error(request, "没有待发布的草稿。")
                return redirect("policy_detail", pk=pk)
            draft.is_draft = False
            draft.save(update_fields=["is_draft"])
            item.published_revision = draft
            item.status = Publication.PUBLISHED
        elif action == "withdraw":
            item.status = Publication.WITHDRAWN
        else:
            raise Http404
        item.save()
        audit(request.user, f"制度：{action}", item)
    messages.success(request, "制度已发布。" if action == "publish" else "制度已撤销，学生无法继续查看和下载。")
    return redirect("policy_detail", pk=pk)


@login_required
@require_safe
@xframe_options_sameorigin
def policy_file(request, pk):
    revision = get_object_or_404(PolicyRevision.objects.select_related("policy"), pk=pk)
    if not request.user.is_staff and not (revision.policy.status == Publication.PUBLISHED and revision.policy.published_revision_id == revision.pk):
        raise Http404
    return send_private_file(request, revision.attachment, "application/pdf", f"{revision.title}.pdf", request.GET.get("download") == "1")


@login_required
def leave_list(request):
    form = LeaveFilterForm(request.GET)
    qs = filtered_leaves(request.user, form)
    return render(request, "portal/leave_list.html", {**page_context(request, qs), "filter_form": form})


@staff_only
@require_safe
def leave_export(request):
    form = LeaveFilterForm(request.GET)
    qs = filtered_leaves(request.user, form)
    qs, sorting = sort_for_page(request, qs, "leave_list")
    if not form.is_valid() or sorting.get("table_sort_error"):
        return render(request, "portal/leave_list.html", {**page_context(request, qs), **sorting, "filter_form": form}, status=400)
    response = StreamingHttpResponse(leave_csv_rows(qs), content_type="text/csv; charset=utf-8")
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    response["Content-Disposition"] = f'attachment; filename="leave-records-{stamp}.csv"'
    if request.method == "GET":
        data = form.cleaned_data
        scope = f"Leave: start={data['start_date'] or '*'}; end={data['end_date'] or '*'}; status={data['status'] or '*'}; q={data['q']}"
        Audit.objects.create(actor=request.user, action="请求导出请假数据", target=scope[:200])
    return response


@student_only
def leave_create(request):
    previous = None
    initial = {}
    if source := query_id(request, "from"):
        previous = get_object_or_404(Leave, pk=source, student=request.user, status=Leave.Status.REJECTED)
        initial = model_to_dict(previous, fields=LeaveForm.Meta.fields)
    form = LeaveForm(request.POST if request.method == "POST" else None, request.FILES, initial=initial, user=request.user)
    if request.method == "POST" and form.is_valid():
        with rollback_uploads() as uploads, transaction.atomic():
            item = form.save(commit=False)
            item.student = request.user
            item.student_name = form.cleaned_data["account_name"]
            item.student_number = form.cleaned_data["account_number"]
            item.destination = f"{item.province} {item.city}"
            item.previous = previous
            item.save()
            for upload in form.cleaned_data["attachments"]:
                uploads.save(LeaveAttachment(leave=item, file=upload, original_name=upload.name))
            LeaveEvent.objects.create(leave=item, actor=request.user, status=Leave.Status.PENDING, note="提交请假申请")
            from .inbox import deliver
            deliver('leave', item, request.user, f'leave:{item.pk}:pending',
                f'{request.user.display_name}提交请假申请 #{item.pk}', to_admins=True)
            audit(request.user, "提交请假", item)
        messages.success(request, "请假申请已提交，请等待管理员审核。")
        return redirect("leave_detail", pk=item.pk)
    return form_page(request, form, "重新提交请假" if previous else "提交请假申请", reverse("leave_list"),
                     "待审核时可以撤销。请假获批后，请在线下完成销假，再由管理员线上确认。", "提交申请")


def owned_leave(request, pk):
    qs = Leave.objects.select_related("student", "previous")
    if not request.user.is_staff:
        qs = qs.filter(student=request.user)
    return get_object_or_404(qs, pk=pk)


@login_required
def leave_detail(request, pk):
    item = owned_leave(request, pk)
    from .inbox import topic_context
    return render(request, "portal/leave_detail.html", {"item": item, "events": item.events.select_related("actor"),
        **topic_context(request.user, 'leave', item.pk)})


@login_required
@require_safe
def leave_file(request, leave_pk, pk):
    item = owned_leave(request, leave_pk)
    attachment = get_object_or_404(item.attachments, pk=pk)
    return send_private_file(request, attachment.file, "application/octet-stream", attachment.original_name, download=True)


@login_required
@require_POST
def leave_action(request, pk, action):
    owned_leave(request, pk)
    try:
        transition_leave(pk, request.user, action, request.POST.get("note", ""))
        messages.success(request, "办理状态已更新。")
    except ValidationError as exc:
        messages.error(request, "；".join(exc.messages))
    return redirect("leave_detail", pk=pk)


@login_required
def support_home(request):
    return redirect("faq_list")


@login_required
def faq_list(request):
    form = FAQFilterForm(request.GET, user=request.user)
    qs = FAQ.objects.all()
    if not request.user.is_staff:
        qs = qs.filter(status=Publication.PUBLISHED)
    if form.is_valid():
        data = form.cleaned_data
        if data["q"]:
            qs = qs.filter(Q(question__icontains=data["q"]) | Q(answer__icontains=data["q"]))
        if data["category"]:
            qs = qs.filter(category=data["category"])
        if data.get("status"):
            qs = qs.filter(status=data["status"])
    else:
        qs = qs.none()
    selected = request.GET.get("category", "")
    pagination = content_page(request, qs, page_context(request, qs), "faq")
    return render(request, "portal/faq_list.html", {**pagination, "filter_form": form,
        "category_tabs": category_tabs(request, InquiryCategory.choices, "faq_list", "全部类别"),
        "selected_category": selected if selected in InquiryCategory.values else ""})


@login_required
def faq_detail(request, pk):
    qs = FAQ.objects.all() if request.user.is_staff else FAQ.objects.filter(status=Publication.PUBLISHED)
    item = get_object_or_404(qs, pk=pk)
    from .analytics import detail_metrics
    return render(request, "portal/faq_detail.html", {"item": item, **detail_metrics(request, "faq", item)})


@staff_only
def faq_edit(request, pk=None):
    with transaction.atomic():
        item = get_object_or_404(FAQ.objects.select_for_update(), pk=pk) if pk else None
        source = item.source if item else None
        category = request.GET.get("category")
        initial = {"category": category} if not item and category in InquiryCategory.values else {}
        if not item and (source_id := query_id(request, "source")):
            source = get_object_or_404(Conversation, pk=source_id)
            replies = source.messages.filter(author__is_staff=True)
            if not replies.exists():
                messages.info(request, "请先回复该咨询，再整理为官方问答。")
                return redirect("consultation_detail", pk=source.pk)
            initial = {"question": source.subject, "category": source.category,
                       "answer": "\n\n".join(replies.values_list("body", flat=True))}
        form = FAQForm(request.POST or None, instance=item, initial=initial)
        if request.method == "POST" and form.is_valid():
            item = form.save(commit=False)
            if not item.pk:
                item.created_by = request.user
                item.source = source
            item.status = Publication.DRAFT
            item.save()
            audit(request.user, "保存问答草稿", item)
            messages.success(request, "问答已保存为草稿，请确认公开内容后发布。")
            return redirect("faq_detail", pk=item.pk)
        return form_page(request, form, "编辑官方问答" if item else "新增官方问答", reverse("faq_list"),
                         "发布前请删除姓名、学号、电话等个人信息；仅本次填写的问答正文会对学生公开。", "保存并预览")


@staff_only
@require_POST
def faq_action(request, pk, action):
    with transaction.atomic():
        item = get_object_or_404(FAQ.objects.select_for_update(), pk=pk)
        if action == "publish":
            item.status = Publication.PUBLISHED
        elif action == "withdraw":
            item.status = Publication.WITHDRAWN
        elif action == "cancel" and item.status == Publication.DRAFT:
            audit(request.user, "取消问答草稿", item)
            item.delete()
            messages.success(request, "已取消该问答草稿。")
            return redirect("faq_list")
        else:
            raise Http404
        item.save(update_fields=["status", "updated_at"])
        audit(request.user, f"问答：{action}", item)
        messages.success(request, "问答状态已更新。")
        return redirect("faq_detail", pk=pk)


@login_required
def consultation_list(request):
    form = ConsultationFilterForm(request.GET)
    qs = Conversation.objects.select_related("student", "matter")
    if not request.user.is_staff:
        qs = qs.filter(student=request.user)
    if form.is_valid():
        data = form.cleaned_data
        if data["waiting"]:
            qs = qs.filter(awaiting_reply=data["waiting"] == "1")
        if data["category"]:
            qs = qs.filter(category=data["category"])
        if data["q"]:
            qs = qs.filter(Q(subject__icontains=data["q"]) | Q(student__username__icontains=data["q"]) |
                           Q(student__full_name__icontains=data["q"]) | Q(messages__body__icontains=data["q"])).distinct()
    else:
        qs = qs.none()
    selected = request.GET.get("category", "")
    return render(request, "portal/consultation_list.html", {**page_context(request, qs), "filter_form": form,
        "category_tabs": category_tabs(request, InquiryCategory.choices, "consultation_list", "全部类别"),
        "selected_category": selected if selected in InquiryCategory.values else ""})


@student_only
def consultation_create(request):
    if request.GET.get("matter"):
        raise Http404
    category = request.GET.get("category")
    initial = {"category": category} if category in InquiryCategory.values else {}
    form = ConversationForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            item = Conversation.objects.create(student=request.user, subject=form.cleaned_data["subject"], category=form.cleaned_data["category"])
            message = Message.objects.create(conversation=item, author=request.user, body=form.cleaned_data["body"])
            from .inbox import deliver
            deliver('consultation', item, request.user, f'message:{message.pk}',
                f'新咨询：{item.subject}', to_admins=True)
        messages.success(request, "咨询已发送，请等待管理员回复。")
        return redirect("consultation_detail", pk=item.pk)
    return form_page(request, form, "发起咨询", reverse("consultation_list"),
                     "咨询内容仅本人和管理员可见。", "发送咨询", consultation_guidance="compose")


@login_required
def consultation_detail(request, pk):
    qs = Conversation.objects.select_related("student", "matter")
    if not request.user.is_staff:
        qs = qs.filter(student=request.user)
    item = get_object_or_404(qs, pk=pk)
    from .inbox import topic_context
    read_context = topic_context(request.user, 'consultation', item.pk)
    form = MessageForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            locked = Conversation.objects.select_for_update()
            if not request.user.is_staff:
                locked = locked.filter(student=request.user)
            item = get_object_or_404(locked, pk=pk)
            is_followup = not request.user.is_staff and item.messages.filter(author__is_staff=True).exists()
            message = Message.objects.create(conversation=item, author=request.user, body=form.cleaned_data["body"])
            item.awaiting_reply = not request.user.is_staff
            item.save(update_fields=["awaiting_reply", "updated_at"])
            from .inbox import deliver
            deliver('consultation', item, request.user, f'message:{message.pk}',
                ('咨询收到回复：' if request.user.is_staff else '咨询收到追问 / 补充：')+item.subject,
                to_admins=not request.user.is_staff)
        messages.success(request, "回复已发送。" if request.user.is_staff else
            "追问 / 补充内容已发送，等待管理员回复。" if is_followup else "补充内容已发送，等待管理员回复。")
        return redirect("consultation_detail", pk=pk)
    thread = list(item.messages.select_related("author"))
    has_admin_reply = any(message.author.is_staff for message in thread)
    form.fields["body"].label = "回复内容" if request.user.is_staff else "追问 / 补充说明" if has_admin_reply else "补充说明"
    form.fields["body"].widget.attrs["placeholder"] = ("请针对学生的问题填写回复。" if request.user.is_staff else
        "请围绕本次咨询继续追问，补充需要说明的情况。" if has_admin_reply else "如有需要说明的情况，可在这里补充。")
    return render(request, "portal/consultation_detail.html", {"item": item, "form": form,
                  "thread": thread, "has_admin_reply": has_admin_reply, "can_curate": request.user.is_staff and has_admin_reply, **read_context})


@login_required
def video_list(request):
    form = VideoFilterForm(request.GET)
    qs = Video.objects.filter(is_deleted=False)
    if form.is_valid():
        data = form.cleaned_data
        if data["category"]:
            qs = qs.filter(category=data["category"])
        if q := data["q"]:
            categories = [value for value, label in Video.Category.choices if q.casefold() in label.casefold()]
            qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q) | Q(category__in=categories))
    else:
        qs = qs.none()
    selected = request.GET.get("category", "")
    pagination = content_page(request, qs, page_context(request, qs), "video")
    return render(request, "portal/video_list.html", {**pagination, "filter_form": form,
        "category_tabs": category_tabs(request, Video.Category.choices, "video_list", "全部类别"),
        "selected_category": selected if selected in Video.Category.values else ""})


@staff_only
def video_create(request):
    category = request.GET.get("category", "")
    form = VideoForm(request.POST or None, request.FILES or None,
                     initial={"category": category if category in Video.Category.values else ""})
    if request.method == "POST" and form.is_valid():
        with rollback_uploads() as uploads, transaction.atomic():
            item = form.save(commit=False)
            item.uploaded_by = request.user
            uploads.save(item)
            audit(request.user, "上传视频", item)
            messages.success(request, "视频已上传并发布。")
            return redirect("video_detail", pk=item.pk)
    return form_page(request, form, "上传学习视频", reverse("video_list"),
                     "请使用 H.264 视频 / AAC 音频的 MP4，单个文件最多 500 MB，以保证常用浏览器兼容。", "上传并发布")


@staff_only
def video_edit(request, pk):
    item = get_object_or_404(Video, pk=pk, is_deleted=False)
    form = VideoMetadataForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        changed = Video.objects.filter(pk=item.pk, is_deleted=False).update(title=item.title, category=item.category,
            description=item.description, updated_at=timezone.now())
        if not changed:
            raise Http404
        audit(request.user, "修改视频信息", item)
        messages.success(request, "视频标题、类别和说明已更新。")
        return redirect("video_detail", pk=item.pk)
    return form_page(request, form, "编辑视频信息", reverse("video_list"), "可修改标题、类别和内容介绍。", "保存修改")


@login_required
def video_detail(request, pk):
    item = get_object_or_404(Video, pk=pk, is_deleted=False)
    from .analytics import detail_metrics
    return render(request, "portal/video_detail.html", {"item": item, **detail_metrics(request, "video", item)})


@login_required
@require_safe
def video_file(request, pk):
    item = get_object_or_404(Video, pk=pk, is_deleted=False)
    return send_private_file(request, item.file, "video/mp4", f"{item.title}.mp4")


@staff_only
@require_POST
def video_delete(request, pk):
    item = get_object_or_404(Video, pk=pk, is_deleted=False)
    item.is_deleted = True
    item.save(update_fields=["is_deleted", "updated_at"])
    audit(request.user, "删除视频", item)
    messages.success(request, "视频已删除，检索和播放入口均已关闭。")
    return redirect("video_list")


@login_required
def guide_list(request):
    form = ServiceGuideFilterForm(request.GET, user=request.user)
    qs = ServiceGuide.objects.annotate(attachment_count=Count("attachments", distinct=True)).order_by(*ServiceGuide._meta.ordering)
    if not request.user.is_staff:
        qs = qs.filter(status=ServiceGuide.Status.PUBLISHED)
    if form.is_valid():
        data = form.cleaned_data
        if data["category"]:
            qs = qs.filter(category=data["category"])
        if data["q"]:
            qs = qs.filter(Q(title__icontains=data["q"]) | Q(owner__icontains=data["q"]) |
                           Q(contact__icontains=data["q"]) | Q(office__icontains=data["q"]) | Q(process__icontains=data["q"]))
        if data.get("status"):
            qs = qs.filter(status=data["status"])
    else:
        qs = qs.none()
    selected = request.GET.get("category", "")
    pagination = content_page(request, qs, page_context(request, qs), "guide")
    return render(request, "portal/guide_list.html", {**pagination, "filter_form": form,
        "category_tabs": category_tabs(request, ServiceGuide.Category.choices, "guide_list", "全部指南"),
        "selected_category": selected if selected in ServiceGuide.Category.values else ""})


def visible_guide(request, pk):
    qs = ServiceGuide.objects.all()
    if not request.user.is_staff:
        qs = qs.filter(status=ServiceGuide.Status.PUBLISHED)
    return get_object_or_404(qs, pk=pk)


@login_required
def guide_detail(request, pk):
    item = visible_guide(request, pk)
    from .analytics import detail_metrics
    return render(request, "portal/guide_detail.html", {"item": item, "attachments": item.attachments.all(), **detail_metrics(request, "guide", item)})


@staff_only
def guide_edit(request, pk=None):
    with rollback_uploads() as uploads, transaction.atomic():
        item = get_object_or_404(ServiceGuide.objects.select_for_update(), pk=pk) if pk else None
        category = request.GET.get("category")
        initial = {"category": category} if not item and category in ServiceGuide.Category.values else {}
        form = ServiceGuideForm(request.POST or None, request.FILES or None, instance=item, initial=initial)
        if request.method == "POST" and form.is_valid():
            item = form.save(commit=False)
            if not item.pk:
                item.created_by = request.user
                item.status = ServiceGuide.Status.PUBLISHED
            item.updated_by = request.user
            uploads.save(item)
            removed = form.cleaned_data.get("remove_attachments", [])
            item.attachments.filter(pk__in=[attachment.pk for attachment in removed]).delete()
            for upload in form.cleaned_data.get("attachments", []):
                uploads.save(GuideAttachment(guide=item, file=upload, original_name=upload.name))
            audit(request.user, "更新办事指南" if pk else "发布办事指南", item)
            messages.success(request, "指南已更新，仍保持撤下状态。" if item.status == ServiceGuide.Status.WITHDRAWN else "办事指南已保存并发布。")
            return redirect("guide_detail", pk=item.pk)
    return form_page(request, form, "修改 / 替换办事指南" if item else "发布办事指南", reverse("guide_list"),
        "填写负责人、联系方式、办公地址和正文流程。可上传多个 Excel、Word 或 PDF 附件；撤下的指南修改后不会自动重新发布。",
        "保存修改" if item else "发布办事指南")


@staff_only
@require_POST
def guide_action(request, pk, action):
    if action not in {"publish", "withdraw"}:
        raise Http404
    target = ServiceGuide.Status.PUBLISHED if action == "publish" else ServiceGuide.Status.WITHDRAWN
    with transaction.atomic():
        item = get_object_or_404(ServiceGuide.objects.select_for_update(), pk=pk)
        if item.status == target:
            messages.info(request, "该指南已经处于所选状态。")
        else:
            item.status = target
            item.updated_by = request.user
            if action == "publish":
                item.published_at = timezone.now()
            item.save()
            audit(request.user, "重新发布办事指南" if action == "publish" else "撤下办事指南", item)
            messages.success(request, "办事指南已重新发布。" if action == "publish" else "办事指南已撤下，学生无法查看正文和附件。")
    return redirect("guide_detail", pk=pk)


@login_required
@require_safe
@xframe_options_sameorigin
def guide_file(request, guide_pk, pk):
    guide = visible_guide(request, guide_pk)
    attachment = get_object_or_404(guide.attachments, pk=pk)
    content_type = GUIDE_FILE_TYPES.get(attachment.extension)
    if not content_type:
        raise Http404
    return send_private_file(request, attachment.file, content_type, attachment.original_name,
                             download=not attachment.is_pdf or request.GET.get("download") == "1")


@login_required
def attendance_list(request):
    from .attendance_queries import filtered_attendance
    form, qs, params, selected_sort, direction = filtered_attendance(request)
    # 排序和分页链接使用实际生效的默认值/兼容转换，避免翻页后变更查询范围。
    for key in list(params):
        if key not in form.fields:
            params.pop(key, None)
    pagination = page_context(request, qs, params=params)
    params["per_page"] = str(pagination["per_page"])
    columns = []
    for key, label in ATTENDANCE_SORT_CHOICES:
        links = []
        for value, text in [("asc", "升序"), ("desc", "降序")]:
            query = params.copy()
            query["sort"], query["direction"] = key, value
            links.append({"direction": value, "text": text, "url": "?" + query.urlencode(),
                          "active": form.is_valid() and selected_sort == key and direction == value})
        columns.append({"key": key, "label": label, "links": links,
                        "aria_sort": ("ascending" if direction == "asc" else "descending") if form.is_valid() and selected_sort == key else "none"})
    totals = qs.aggregate(clock=Sum("clock_days", default=0), leave=Sum("leave_days", default=0))
    return render(request, "portal/attendance_list.html", {**pagination,
        "filter_form": form, "sort_columns": columns, "student_count": qs.order_by().values("student_id").distinct().count(),
        "totals": [("打卡天数", totals["clock"]), ("请假天数", totals["leave"]), ("总天数", totals["clock"] + totals["leave"])],
        "batches": AttendanceBatch.objects.filter(is_confirmed=True, format_version=2)[:5] if request.user.is_staff else []})


@staff_only
def attendance_upload(request):
    form = AttendanceUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            rows = parse_attendance(form.cleaned_data["file"])
        except ValidationError as exc:
            form.add_error("file", exc)
        else:
            months = {row["month"] for row in rows}
            month = date.fromisoformat(next(iter(months)) + "-01") if len(months) == 1 else None
            batch = AttendanceBatch.objects.create(month=month, uploaded_by=request.user, row_count=len(rows), payload=rows)
            return redirect("attendance_preview", pk=batch.pk)
    return form_page(request, form, "导入月度打卡", reverse("attendance_list"),
                     "每行是一名学生一个月的汇总。先校验预览，确认后写入；相同学号和月份的记录会被更新。", "校验并预览",
                     template_download=reverse("attendance_template"),
                     template_hint="填写学号、姓名、月份、打卡记录、请假天数；月份例如 2026-11，打卡记录填写当月打卡天数，两种天数均为非负整数。")


@staff_only
def attendance_preview(request, pk):
    batch = get_object_or_404(AttendanceBatch, pk=pk, uploaded_by=request.user)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "cancel":
            with transaction.atomic():
                batch = get_object_or_404(AttendanceBatch.objects.select_for_update(), pk=pk, uploaded_by=request.user)
                if batch.is_confirmed:
                    messages.error(request, "该批次已导入，不能取消。")
                else:
                    batch.delete()
                    messages.info(request, "已取消本次导入，没有修改打卡记录。")
                    return redirect("attendance_list")
        elif action == "confirm":
            first_month = batch.payload[0].get("month") if batch.format_version == 2 and batch.payload else None
            try:
                confirm_attendance(batch.pk, request.user)
            except ValidationError as exc:
                messages.error(request, "；".join(exc.messages))
                if not AttendanceBatch.objects.filter(pk=pk, uploaded_by=request.user).exists():
                    return redirect('attendance_list')
            else:
                messages.success(request, f"已导入 {batch.row_count} 条月度打卡记录。")
                query = f"?year={first_month[:4]}&month={int(first_month[5:])}" if first_month else ""
                return redirect(reverse("attendance_list") + query)
        else:
            messages.error(request, "请选择确认导入或取消导入。")
        return redirect("attendance_preview", pk=pk)
    context = page_context(request, batch.payload if batch.format_version == 2 else [], per_page=100)
    return render(request, "portal/attendance_preview.html", {**context, "batch": batch, "rows": [{**row, "masked_id_number": row.get("id_number", "")[:6] + "********" + row.get("id_number", "")[-4:], "political_label": dict(User.Politics.choices).get(row.get("political_status", ""), "未填写")} for row in context["page_obj"]],
        "expired": batch.created_at < timezone.now() - timedelta(days=1)})


@staff_only
@require_safe
def attendance_template(request):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="monthly-attendance-template.csv"'
    response.write("\ufeff")
    csv.writer(response).writerow(["学号", "姓名", "月份", "打卡记录", "请假天数"])
    return response


def _save_student_preview(actor, rows, mode):
    batch = StudentImportBatch.objects.create(uploaded_by=actor, row_count=len(rows), payload=rows, mode=mode)
    return reverse("account_preview", args=[batch.pk])


def _student_preview_events(actor, total, chunks):
    rows = []
    yield json.dumps({"completed": 0, "total": total}) + "\n"
    for chunk in chunks:
        rows.extend(chunk)
        yield json.dumps({"completed": len(rows), "total": total}) + "\n"
    yield json.dumps({"preview": _save_student_preview(actor, rows, "create")}) + "\n"


@staff_only
def account_upload(request):
    stream = request.method == "POST" and request.headers.get("Accept") == "application/x-ndjson"
    form = StudentImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        mode = form.cleaned_data["mode"]
        try:
            total, chunks = prepare_student_account_import(form.cleaned_data["file"], mode)
        except ValidationError as exc:
            form.add_error("file", exc)
        else:
            if stream and mode == "create":
                response = StreamingHttpResponse(_student_preview_events(request.user, total, chunks),
                                                 content_type="application/x-ndjson")
                response["X-Accel-Buffering"] = "no"
                return response
            rows = [row for chunk in chunks for row in chunk]
            return redirect(_save_student_preview(request.user, rows, mode))
    if stream:
        return JsonResponse({"errors": [error for errors in form.errors.values() for error in errors]}, status=400)
    return form_page(request, form, "批量导入学生信息", reverse("account_list"),
        "新增模式创建账号；更新模式按学号定位，只修改 CSV 中保留的资料列。两种方式均先校验预览，再整批确认。",
        "校验并预览", template_download=reverse("account_template"),
        template_hint="新增时请填写学号、姓名、身份证号、在校状态和初始密码。所在班级、党支部、政治面貌和院系选填。学号列使用文本格式保留前导零，保存为 UTF-8 CSV。",
        student_import=True)


@staff_only
def account_preview(request, pk):
    batch = get_object_or_404(StudentImportBatch, pk=pk, uploaded_by=request.user)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "cancel":
            with transaction.atomic():
                batch = get_object_or_404(StudentImportBatch.objects.select_for_update(), pk=pk, uploaded_by=request.user)
                if batch.is_confirmed:
                    messages.error(request, "该批次已导入，不能取消。")
                else:
                    audit(request.user, "取消学生账号导入", batch)
                    batch.delete()
                    messages.info(request, "已取消本次导入，没有创建账号。")
                    return redirect("account_list")
        elif action == "confirm":
            try:
                confirm_student_accounts(batch.pk, request.user)
            except ValidationError as exc:
                messages.error(request, "；".join(exc.messages))
                if not StudentImportBatch.objects.filter(pk=pk, uploaded_by=request.user).exists():
                    return redirect('account_list')
            else:
                messages.success(request, f"已更新 {batch.row_count} 名学生资料，密码、权限和登录状态保持不变。" if batch.mode == "update" else f"已创建 {batch.row_count} 个学生账号，请线下告知各学生的初始密码。")
                return redirect("account_list")
        else:
            messages.error(request, "请选择确认导入或取消导入。")
        return redirect("account_preview", pk=pk)
    context = page_context(request, batch.payload, per_page=100)
    update_fields = batch.payload[0].get("update_fields", list(STUDENT_IMPORT_COLUMNS.values())[1:]) if batch.payload and batch.mode == "update" else []
    update_labels = "、".join(header for header, field in STUDENT_IMPORT_COLUMNS.items() if field in update_fields)
    return render(request, "portal/account_preview.html", {**context, "batch": batch, "rows": [{**row, "masked_id_number": row["id_number"][:6] + "********" + row["id_number"][-4:] if row.get("id_number") else "待补全", "political_label": dict(User.Politics.choices).get(row.get("political_status", ""), "未填写"), "enrollment_label": dict(User.EnrollmentStatus.choices).get(row.get("enrollment_status", ""), "待补全")} for row in context["page_obj"]],
        "expired": batch.created_at < timezone.now() - timedelta(days=1), "update_labels": update_labels})


@staff_only
@require_safe
def account_template(request):
    politics_update = request.GET.get("mode") == "update"
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    filename = "student-politics-update-template.csv" if politics_update else "student-accounts-template.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")
    csv.writer(response).writerow(STUDENT_POLITICS_UPDATE_HEADERS if politics_update else STUDENT_IMPORT_HEADERS)
    return response


@staff_only
def account_reset(request, pk):
    with transaction.atomic():
        item = get_object_or_404(User.objects.select_for_update(), pk=pk, is_staff=False, is_superuser=False)
        form = SetPasswordForm(item, request.POST or None)
        if request.method == "POST" and form.is_valid():
            student = form.save(commit=False)
            student.online_until = None
            student.password_change_required = True
            student.save(update_fields=["password", "online_until", "password_change_required"])
            User.objects.filter(pk=student.pk).update(session_revision=F('session_revision')+1)
            audit(request.user, "重置学生密码", item)
            messages.success(request, "密码已重置，原登录会话将失效。请线下告知临时密码，学生登录后须先设置自己的新密码。")
            return redirect("account_list")
        return form_page(request, form, f"重置密码 · {item.display_name}", reverse("account_list"),
                         "请先在线下核实学生身份，再设置新密码。", "重置密码")


@staff_only
def audit_list(request):
    return render(request, "portal/audit_list.html", page_context(request, Audit.objects.select_related("actor")))
