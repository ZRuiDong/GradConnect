import hashlib
import json


STUDENT_PROFILE_COLUMNS = ("full_name", "id_number", "enrollment_status", "party_branch", "student_class", "political_status", "department")


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")).encode()).hexdigest()


def student_profile_digest(student):
    return _digest([student.pk, student.username, *[getattr(student, field) for field in STUDENT_PROFILE_COLUMNS]])


def student_edit_digest(student):
    cached = getattr(student, "_prefetched_objects_cache", {}).get("cadre_tenures")
    tenures = ([(item.pk, item.position, item.starts_on, item.ends_on, item.updated_at) for item in sorted(cached, key=lambda item: item.pk)]
               if cached is not None else list(student.cadre_tenures.order_by("pk").values_list("pk", "position", "starts_on", "ends_on", "updated_at")))
    return _digest([student_profile_digest(student), student.password, student.is_active, student.is_archived,
                    student.session_revision, student.password_change_required, tenures])
