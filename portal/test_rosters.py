import io
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile
import openpyxl
from openpyxl.utils.datetime import to_excel
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from .forms import CadreTenureForm, StudentCreationForm
from .models import CadreTenure, StudentRosterBatch, User
from .roster_imports import HEADERS, confirm_roster, parse_roster
from .student_services import student_queryset
from .student_state import student_edit_digest


def excel_fixture(kind, rows, headers=None):
    """在已生成的模板中注入测试XML数据，不依赖Excel客户端。"""
    path = Path(settings.BASE_DIR) / "portal" / "import_templates" / f"student-{kind}.xlsx"
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    root = ET.Element(f"{{{ns}}}worksheet")
    ET.SubElement(root, f"{{{ns}}}dimension", {"ref": f"A1:{chr(64 + len(HEADERS[kind]))}{len(rows)+1}"})
    data = ET.SubElement(root, f"{{{ns}}}sheetData")
    for index, values in enumerate([HEADERS[kind] if headers is None else headers, *rows], 1):
        row = ET.SubElement(data, f"{{{ns}}}row", {"r": str(index)})
        for col, value in enumerate(values):
            cell = ET.SubElement(row, f"{{{ns}}}c", {"r": f"{chr(65+col)}{index}"})
            if value is None:
                continue
            if isinstance(value, (date, datetime)):
                cell.set("s", "1")
                ET.SubElement(cell, f"{{{ns}}}v").text = str(to_excel(value))
            elif isinstance(value, (int, float)):
                ET.SubElement(cell, f"{{{ns}}}v").text = str(value)
            elif value.startswith("="):
                ET.SubElement(cell, f"{{{ns}}}f").text = value[1:]
            else:
                cell.set("t", "inlineStr")
                ET.SubElement(ET.SubElement(cell, f"{{{ns}}}is"), f"{{{ns}}}t").text = value
    output = io.BytesIO()
    with ZipFile(path) as original, ZipFile(output, "w") as changed:
        for member in original.infolist():
            content = ET.tostring(root) if member.filename == "xl/worksheets/sheet1.xml" else original.read(member.filename)
            if member.filename == "xl/styles.xml":
                content = b'<?xml version="1.0"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="1"><font/></fonts><fills count="1"><fill><patternFill patternType="none"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14" applyNumberFormat="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
            changed.writestr(member, content)
    return SimpleUploadedFile("test.xlsx", output.getvalue())


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class StudentRosterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(username="roster-manager", is_staff=True, full_name="管理员")
        cls.other_admin = User.objects.create_user(username="other-manager", is_staff=True)
        cls.student = User.objects.create_user(username="000001", full_name="学生甲", password="Original!Maple2026",
            id_number="110000199001010001", student_class="甲班", party_branch="", graduation_month=date(2025, 6, 1))
        cls.other = User.objects.create_user(username="000002", full_name="学生乙")

    def setUp(self):
        self.client.force_login(self.admin)
        self.today = timezone.localdate()

    def batch(self, kind="graduate", rows=None):
        rows = rows if rows is not None else [[self.student.username, self.student.full_name]]
        payload = parse_roster(excel_fixture(kind, rows), kind)
        return StudentRosterBatch.objects.create(uploaded_by=self.admin, kind=kind, row_count=len(payload), payload=payload)

    def tenure(self, name, start, end):
        return CadreTenure.objects.create(student=self.student, position=name, starts_on=start, ends_on=end)

    def test_status_replaces_graduation_month_in_forms_list_and_filters(self):
        self.assertEqual(self.student.enrollment_status, "enrolled")
        self.assertNotIn("graduation_month", StudentCreationForm().fields)
        self.assertIn("enrollment_status", StudentCreationForm().fields)
        User.objects.filter(pk=self.other.pk).update(enrollment_status="graduated")
        for status, expected in [("enrolled", self.student), ("graduated", self.other)]:
            response = self.client.get(reverse("account_list"), {"enrollment_status": status})
            self.assertEqual(list(response.context["page_obj"]), [expected])
            self.assertNotContains(response, "毕业年月")
            self.assertContains(response, "批量修改毕业学生")
        self.student.refresh_from_db()
        self.assertEqual(self.student.graduation_month, date(2025, 6, 1))

    def test_all_current_positions_include_both_boundaries_and_exclude_past_future(self):
        active = self.tenure("班长", self.today - timedelta(days=20), self.today + timedelta(days=20))
        self.tenure("今日开始", self.today, date(2999, 12, 31))
        self.tenure("今日结束", self.today - timedelta(days=2), self.today)
        self.tenure("旧任职", self.today - timedelta(days=5), self.today - timedelta(days=1))
        self.tenure("未来任职", self.today + timedelta(days=1), date(2999, 12, 31))
        self.tenure("原未设期限", self.today - timedelta(days=3), None)
        item = student_queryset().get(pk=self.student.pk)
        self.assertEqual({role.position for role in item.current_tenures}, {"班长", "今日开始", "今日结束", "原未设期限"})
        self.assertTrue(active.is_current)
        response = self.client.get(reverse("account_list"), {"q": self.student.username})
        table = response.content.decode().split('<tbody>')[1].split('</tbody>')[0]
        self.assertNotIn("旧任职", table)
        self.assertNotIn("未来任职", table)
        self.assertIn("班长", table)
        self.assertIn("今日结束", table)
        detail = self.client.get(reverse("account_detail", args=[self.student.pk]))
        self.assertContains(detail, "旧任职")
        self.assertContains(detail, "未来任职")

    def test_current_role_multi_filter_and_search_match_any_current_role_without_duplicates(self):
        self.tenure("班长", self.today - timedelta(days=20), date(2999, 12, 31))
        self.tenure("学习委员", self.today - timedelta(days=10), date(2999, 12, 31))
        self.tenure("历史职务", self.today - timedelta(days=5), self.today - timedelta(days=1))
        for params in [{"current_position": ["班长", "学习委员"]}, {"q": "学习委员 甲班"}, {"current_position": ["学习委员"], "enrollment_status": "enrolled"}]:
            self.assertEqual(list(self.client.get(reverse("account_list"), params).context["page_obj"]), [self.student])
        self.assertEqual(self.client.get(reverse("account_list"), {"q": "历史职务"}).context["page_obj"].paginator.count, 0)

    def test_manual_tenure_default_and_edit_dates_change_current_display(self):
        form = CadreTenureForm(data={"position": "班长", "starts_on": self.today.isoformat(), "ends_on": ""})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["ends_on"], date(2999, 12, 31))
        self.assertEqual(CadreTenureForm().fields["ends_on"].initial, date(2999, 12, 31))
        item = self.tenure("班长", self.today, date(2999, 12, 31))
        edit = self.client.get(reverse("account_edit", args=[self.student.pk]))
        response = self.client.post(reverse("account_edit", args=[self.student.pk]), {"full_name": self.student.full_name,
            "id_number": self.student.id_number, "enrollment_status": "enrolled", "party_branch": "", "student_class": "甲班", "is_active": "on",
            "edit_token": edit.context["edit_token"], "tenures-TOTAL_FORMS": "1", "tenures-INITIAL_FORMS": "1", "tenures-MAX_NUM_FORMS": "100",
            "tenures-0-id": str(item.pk), "tenures-0-position": "班长", "tenures-0-starts_on": (self.today - timedelta(days=2)).isoformat(),
            "tenures-0-ends_on": (self.today - timedelta(days=1)).isoformat()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(student_queryset().get(pk=self.student.pk).current_tenures, [])

    def test_templates_are_excel_readable_and_management_only(self):
        for kind in HEADERS:
            response = self.client.get(reverse("roster_template", args=[kind]))
            raw = b"".join(response.streaming_content)
            response.close()
            book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True)
            self.assertEqual(list(next(book.worksheets[0].iter_rows(values_only=True))), HEADERS[kind])
            self.assertEqual(book.sheetnames[1], "填写说明")
            book.close()
            self.client.force_login(self.student)
            self.assertEqual(self.client.get(reverse("roster_template", args=[kind])).status_code, 403)
            self.assertEqual(self.client.get(reverse("roster_upload", args=[kind])).status_code, 403)
            self.client.force_login(self.admin)

    def test_graduation_confirmation_changes_only_status_and_keeps_history(self):
        self.tenure("班长", self.today, date(2999, 12, 31))
        before = User.objects.values().get(pk=self.student.pk)
        batch = self.batch()
        self.assertEqual(User.objects.get(pk=self.student.pk).enrollment_status, "enrolled")
        self.assertEqual(confirm_roster(batch.pk, self.admin), 1)
        self.assertEqual(User.objects.values().get(pk=self.student.pk), {**before, "enrollment_status": "graduated"})
        self.assertEqual(self.student.cadre_tenures.count(), 1)
        self.assertEqual(User.objects.get(pk=self.other.pk).enrollment_status, "enrolled")
        batch.refresh_from_db()
        self.assertEqual(batch.payload, [])
        with self.assertRaisesMessage(ValidationError, "请勿重复确认"):
            confirm_roster(batch.pk, self.admin)
        self.assertEqual(confirm_roster(self.batch().pk, self.admin), 0)

    def test_graduation_http_preview_cancel_ownership_and_csrf(self):
        response = self.client.post(reverse("roster_upload", args=["graduate"]), {"file": excel_fixture("graduate", [[self.student.username, self.student.full_name]])})
        batch = StudentRosterBatch.objects.get()
        self.assertRedirects(response, reverse("roster_preview", args=[batch.pk]))
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.admin)
        self.assertEqual(strict.post(response.url, {"action": "confirm"}).status_code, 403)
        self.client.force_login(self.other_admin)
        self.assertEqual(self.client.get(response.url).status_code, 404)
        self.assertEqual(self.client.post(response.url, {"action": "confirm"}).status_code, 404)
        self.client.force_login(self.admin)
        self.assertRedirects(self.client.post(response.url, {"action": "cancel"}), reverse("account_list"))
        self.assertEqual(User.objects.get(pk=self.student.pk).enrollment_status, "enrolled")

    def test_invalid_names_unknown_admin_and_duplicate_rows_reject_entire_list(self):
        for rows in [[["not-found", "无学生"]], [[self.student.username, "错误姓名"]], [[self.admin.username, "管理员"]],
                     [[self.student.username, self.student.full_name]] * 2, [["=1+1", self.student.full_name]],
                     [[self.student.username, ""]]]:
            with self.subTest(rows=rows), self.assertRaises(ValidationError):
                parse_roster(excel_fixture("graduate", rows), "graduate")
        self.assertEqual(User.objects.get(pk=self.student.pk).enrollment_status, "enrolled")

    def test_corrupt_workbooks_headers_and_oversize_are_validation_errors(self):
        files = [SimpleUploadedFile("broken.xlsx", b"not xlsx"), SimpleUploadedFile("broken.xls", b"not xls"),
                 SimpleUploadedFile("bad-ole.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(504)),
                 SimpleUploadedFile("wrong.csv", b"wrong"), excel_fixture("graduate", [], ["姓名", "学号"]), excel_fixture("graduate", [])]
        large = SimpleUploadedFile("large.xlsx", b"PK")
        large.size = 5 * 1024 * 1024 + 1
        files.append(large)
        for upload in files:
            with self.subTest(upload=upload.name), self.assertRaises(ValidationError):
                parse_roster(upload, "graduate")

    def test_1500_graduation_rows_one_lookup_preview_and_confirmation_from_page_two(self):
        User.objects.bulk_create([User(username=f"graduate-{i:04}", full_name=f"学生{i}", password="unchanged", is_active=False) for i in range(1500)], batch_size=200)
        upload = excel_fixture("graduate", [[f"graduate-{i:04}", f"学生{i}"] for i in range(1500)])
        with self.assertNumQueries(1):
            rows = parse_roster(upload, "graduate")
        batch = StudentRosterBatch.objects.create(kind="graduate", uploaded_by=self.admin, row_count=1500, payload=rows)
        url = reverse("roster_preview", args=[batch.pk])
        preview = self.client.get(url, {"page": 2})
        self.assertEqual(len(preview.context["page_obj"]), 100)
        self.assertContains(preview, "确认处理全部1500条")
        self.client.post(url + "?page=2", {"action": "confirm"})
        self.assertEqual(User.objects.filter(username__startswith="graduate-", enrollment_status="graduated", password="unchanged", is_active=False).count(), 1500)

    def test_preview_conflict_expiry_and_audit_failure_prevent_partial_graduation(self):
        rows = [[self.student.username, self.student.full_name], [self.other.username, self.other.full_name]]
        batch = self.batch(rows=rows)
        User.objects.filter(pk=self.other.pk).update(full_name="新的姓名")
        with self.assertRaisesMessage(ValidationError, "整批未修改"):
            confirm_roster(batch.pk, self.admin)
        self.assertEqual(User.objects.get(pk=self.student.pk).enrollment_status, "enrolled")
        batch = self.batch()
        StudentRosterBatch.objects.filter(pk=batch.pk).update(created_at=timezone.now() - timedelta(days=2))
        with self.assertRaisesMessage(ValidationError, "预览已过期"):
            confirm_roster(batch.pk, self.admin)
        batch = self.batch()
        with patch("portal.roster_imports.Audit.objects.bulk_create", side_effect=IntegrityError("test")), self.assertRaises(IntegrityError):
            confirm_roster(batch.pk, self.admin)
        self.assertEqual(User.objects.get(pk=self.student.pk).enrollment_status, "enrolled")

    def test_tenure_import_defaults_multiple_current_and_future_with_same_student(self):
        rows = [[self.student.username, self.student.full_name, "班长", self.today.isoformat(), ""],
                [self.student.username, self.student.full_name, "学习委员", self.today.isoformat(), ""],
                [self.student.username, self.student.full_name, "未来职务", (self.today + timedelta(days=1)).isoformat(), ""]]
        batch = self.batch("tenure", rows)
        self.assertTrue(all(row["ends_on"] == "2999-12-31" for row in batch.payload))
        self.assertEqual(confirm_roster(batch.pk, self.admin), 3)
        self.assertEqual({role.position for role in student_queryset().get(pk=self.student.pk).current_tenures}, {"班长", "学习委员"})
        self.assertEqual(self.student.cadre_tenures.count(), 3)

    def test_reimport_same_role_updates_end_without_duplicates_and_preserves_others(self):
        old = self.tenure("班长", self.today - timedelta(days=5), date(2999, 12, 31))
        self.tenure("学习委员", self.today, date(2999, 12, 31))
        rows = [[self.student.username, self.student.full_name, "班长", old.starts_on.isoformat(), (self.today - timedelta(days=1)).isoformat()]]
        confirm_roster(self.batch("tenure", rows).pk, self.admin)
        self.assertEqual(self.student.cadre_tenures.count(), 2)
        self.assertEqual([role.position for role in student_queryset().get(pk=self.student.pk).current_tenures], ["学习委员"])

    def test_invalid_tenure_dates_limit_and_duplicate_roles_are_rejected(self):
        valid = [self.student.username, self.student.full_name, "班长", self.today.isoformat(), ""]
        for row in [valid[:3] + ["bad", ""], valid[:3] + [self.today.isoformat(), (self.today - timedelta(days=1)).isoformat()], valid[:2] + ["", self.today.isoformat(), ""]]:
            with self.assertRaises(ValidationError):
                parse_roster(excel_fixture("tenure", [row]), "tenure")
        with self.assertRaises(ValidationError):
            parse_roster(excel_fixture("tenure", [valid, valid]), "tenure")
        CadreTenure.objects.bulk_create([CadreTenure(student=self.student, position=f"职位{i}", starts_on=self.today) for i in range(100)])
        with self.assertRaisesMessage(ValidationError, "超过100条"):
            parse_roster(excel_fixture("tenure", [valid]), "tenure")

    def test_tenure_preview_detects_concurrent_history_and_invalidates_open_edit(self):
        token = self.client.get(reverse("account_edit", args=[self.student.pk])).context["edit_token"]
        rows = [[self.student.username, self.student.full_name, "班长", self.today.isoformat(), ""]]
        batch = self.batch("tenure", rows)
        self.tenure("另一职务", self.today, date(2999, 12, 31))
        with self.assertRaisesMessage(ValidationError, "整批未修改"):
            confirm_roster(batch.pk, self.admin)
        response = self.client.post(reverse("account_edit", args=[self.student.pk]), {"edit_token": token})
        self.assertEqual(response.status_code, 409)

    def test_prefetched_edit_digest_matches_regular_digest_without_more_queries(self):
        self.tenure("班长", self.today, date(2999, 12, 31))
        expected = student_edit_digest(User.objects.get(pk=self.student.pk))
        loaded = User.objects.prefetch_related("cadre_tenures").get(pk=self.student.pk)
        with self.assertNumQueries(0):
            self.assertEqual(student_edit_digest(loaded), expected)

    def test_cleanup_removes_only_expired_unconfirmed_roster_previews(self):
        old = self.batch()
        done = self.batch()
        StudentRosterBatch.objects.filter(pk__in=[old.pk, done.pk]).update(created_at=timezone.now() - timedelta(days=2))
        StudentRosterBatch.objects.filter(pk=done.pk).update(is_confirmed=True)
        fresh = self.batch()
        call_command("cleanup_data", stdout=io.StringIO())
        self.assertFalse(StudentRosterBatch.objects.filter(pk=old.pk).exists())
        self.assertEqual(StudentRosterBatch.objects.filter(pk__in=[done.pk, fresh.pk]).count(), 2)

    def test_1500_tenure_rows_use_prefetched_histories_and_preserve_account_fields(self):
        User.objects.bulk_create([User(username=f"cadre-{i:04}", full_name=f"学生{i}", password="unchanged", is_active=False) for i in range(1500)], batch_size=200)
        upload = excel_fixture("tenure", [[f"cadre-{i:04}", f"学生{i}", "班长", "2020-01-01", ""] for i in range(1500)])
        with self.assertNumQueries(2):
            rows = parse_roster(upload, "tenure")
        batch = StudentRosterBatch.objects.create(kind="tenure", uploaded_by=self.admin, row_count=1500, payload=rows)
        confirm_roster(batch.pk, self.admin)
        self.assertEqual(CadreTenure.objects.filter(student__username__startswith="cadre-", position="班长", ends_on=date(2999, 12, 31)).count(), 1500)
        self.assertEqual(User.objects.filter(username__startswith="cadre-", password="unchanged", is_active=False, enrollment_status="enrolled").count(), 1500)

    def test_graduating_after_concurrent_disable_keeps_disabled_state(self):
        batch = self.batch()
        User.objects.filter(pk=self.student.pk).update(is_active=False, session_revision=1)
        confirm_roster(batch.pk, self.admin)
        self.student.refresh_from_db()
        self.assertEqual((self.student.enrollment_status, self.student.is_active, self.student.session_revision), ("graduated", False, 1))

    def test_number_identifier_and_typed_excel_dates(self):
        user = User.objects.create_user(username="123456", full_name="数字学号")
        rows = parse_roster(excel_fixture("graduate", [[123456, user.full_name]]), "graduate")
        self.assertEqual(rows[0]["username"], "123456")
        rows = parse_roster(excel_fixture("tenure", [[self.student.username, self.student.full_name, "班长", date(2020, 1, 1), date(2999, 12, 31)]]), "tenure")
        self.assertEqual((rows[0]["starts_on"], rows[0]["ends_on"]), ("2020-01-01", "2999-12-31"))
        with self.assertRaises(ValidationError):
            parse_roster(excel_fixture("graduate", [[1, self.student.full_name]]), "graduate")

    def test_legacy_xls_lists_and_typed_dates_are_read_correctly(self):
        for kind in HEADERS:
            raw = (Path(settings.BASE_DIR) / "portal" / "fixtures" / f"test-student-{kind}.xls").read_bytes()
            payload = parse_roster(SimpleUploadedFile("legacy.xls", raw), kind)
            self.assertEqual((payload[0]["username"], payload[0]["name"]), (self.student.username, self.student.full_name))
            if kind == "tenure":
                self.assertEqual((payload[0]["starts_on"], payload[0]["ends_on"]), ("2020-01-01", "2999-12-31"))
