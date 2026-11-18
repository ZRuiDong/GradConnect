import hashlib
import json
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Exists, OuterRef, Prefetch, Q
from django.db.models.deletion import ProtectedError, RestrictedError
from .models import Audit, CadreTenure, User
from .student_state import student_profile_digest
from django.utils import timezone


def current_tenures():
    today = timezone.localdate()
    return CadreTenure.objects.filter(starts_on__lte=today).filter(Q(ends_on__gte=today) | Q(ends_on__isnull=True))


def student_queryset():
    return User.objects.filter(is_staff=False, is_superuser=False).prefetch_related(
        Prefetch("cadre_tenures", queryset=current_tenures().order_by("-starts_on", "-pk"), to_attr="current_tenures")).order_by("username", "pk")


def filtered_students(form):
    qs = student_queryset()
    if not form.is_valid():
        return qs.none()
    data = form.cleaned_data
    if data["archive"] != "all":
        qs = qs.filter(is_archived=data["archive"] == "archived")
    roles = current_tenures().filter(student_id=OuterRef("pk"))
    for term in data["q"].split():
        qs = qs.filter(Q(username__icontains=term) | Q(full_name__icontains=term) | Q(department__icontains=term) |
                       Q(student_class__icontains=term) | Q(party_branch__icontains=term) | Q(Exists(roles.filter(position__icontains=term))))
    for name in ["student_class", "party_branch"]:
        if data[name]:
            qs = qs.filter(**{f"{name}__in": ["" if name == "party_branch" and value == "__empty__" else value for value in data[name]]})
    if data["current_position"]:
        qs = qs.filter(Exists(roles.filter(position__in=data["current_position"])))
    if data["political_status"]:
        qs = qs.filter(political_status__in=["" if value == "__empty__" else value for value in data["political_status"]])
    if data["enrollment_status"]:
        qs = qs.filter(enrollment_status=data["enrollment_status"])
    if data["is_active"]:
        qs = qs.filter(is_active=data["is_active"] == "1")
    if data["cadre"]:
        history = CadreTenure.objects.filter(student_id=OuterRef("pk"))
        qs = qs.filter(Exists(history) if data["cadre"] == "yes" else ~Exists(history))
    missing = Q(full_name="") | Q(id_number="") | Q(enrollment_status="") | Q(student_class="")
    if data["completeness"] == "incomplete":
        qs = qs.filter(missing)
    elif data["completeness"] == "complete":
        qs = qs.exclude(missing)
    return qs


def protected_student_ids(ids):
    protected = set()
    for relation in User._meta.related_objects:
        if getattr(relation.field.remote_field, "on_delete", None) not in {models.PROTECT, models.RESTRICT}:
            continue
        for start in range(0, len(ids), 500):
            rows = relation.related_model.objects.filter(**{relation.field.attname + "__in": ids[start:start + 500]})
            protected.update(rows.order_by().values_list(relation.field.attname, flat=True).distinct())
    return protected


def student_identity_digest(students):
    content = sorted((student.pk, student_profile_digest(student), student.is_active, student.is_archived) for student in students)
    return hashlib.sha256(json.dumps(content, ensure_ascii=False).encode()).hexdigest()


@transaction.atomic
def delete_students(ids, actor, expected_digest):
    if not actor.is_active or not actor.is_staff:
        raise ValidationError("仅管理员可以删除学生。")
    students = list(User.objects.select_for_update().filter(pk__in=ids, is_staff=False, is_superuser=False, is_archived=False))
    if not ids or len(students) != len(set(ids)) or student_identity_digest(students) != expected_digest:
        raise ValidationError("名单或学生身份信息已经变化，请重新筛选并核对。")
    archive_ids = protected_student_ids(ids)
    delete_ids = set(ids) - archive_ids
    if delete_ids:
        try:
            with transaction.atomic():
                User.objects.filter(pk__in=delete_ids, is_staff=False, is_superuser=False).delete()
        except (ProtectedError, RestrictedError):
            # 核对后新产生业务关联时保留账号，避免强制删除业务历史。
            archive_ids.update(delete_ids)
            delete_ids = set()
    if archive_ids:
        User.objects.filter(pk__in=archive_ids, is_staff=False, is_superuser=False).update(
            is_archived=True, is_active=False, online_until=None, session_revision=models.F("session_revision") + 1)
    Audit.objects.bulk_create([Audit(actor=actor, action="归档停用学生（保留历史）" if student.pk in archive_ids else "删除学生（无业务记录）",
                                    target=f"portal.User:{student.pk}") for student in students], batch_size=500)
    return len(delete_ids), len(archive_ids)
