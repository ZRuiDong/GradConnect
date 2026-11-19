import csv
import io
import json
from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from .forms import STUDENT_IMPORT_HEADERS, STUDENT_IMPORT_MAX_BYTES, STUDENT_IMPORT_MAX_ROWS, StudentImportForm
from .models import Audit, StudentImportBatch, User
from .services import confirm_student_accounts, parse_student_accounts


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class StudentAccountImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        password = make_password("Manager!test2026")
        cls.admin = User.objects.create(username="manager", full_name="管理员", is_staff=True, password=password)
        cls.other_admin = User.objects.create(username="other-manager", full_name="另一管理员", is_staff=True, password=password)
        cls.student = User.objects.create(username="20260001", full_name="已有学生", password=password)

    def upload_file(self, rows=None, header=None):
        content = io.StringIO(newline="")
        writer = csv.writer(content)
        writer.writerow(STUDENT_IMPORT_HEADERS if header is None else header)
        source_rows = rows if rows is not None else [
            ["00000003", "新学生甲", "计算机学院", "Initial!Maple2026"],
            ["00000004", "新学生乙", "", "Initial!Cedar2026"],
        ]
        writer.writerows([row[0], row[1], "110000199001010001", "在校", "无", "2026级一班", "群众", row[2], row[3]] if len(row) == 4 else row for row in source_rows)
        return SimpleUploadedFile("students.csv", content.getvalue().encode("utf-8-sig"), content_type="text/csv")

    def create_preview(self, actor=None, rows=None):
        payload = parse_student_accounts(self.upload_file(rows))
        return StudentImportBatch.objects.create(uploaded_by=actor or self.admin, row_count=len(payload), payload=payload)

    def test_upload_preview_confirm_and_login(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("account_list")), reverse("account_upload"))
        self.assertContains(self.client.get(reverse("account_upload")), reverse("account_template"))
        response = self.client.post(reverse("account_upload"), {"file": self.upload_file(), "is_staff": "1", "is_superuser": "1"})
        batch = StudentImportBatch.objects.get()
        self.assertRedirects(response, reverse("account_preview", args=[batch.pk]))
        self.assertFalse(User.objects.filter(username="00000003").exists())
        page = self.client.get(reverse("account_preview", args=[batch.pk]))
        self.assertContains(page, "00000003")
        self.assertContains(page, "新学生乙")
        self.assertNotContains(page, "Initial!Maple2026")
        self.assertNotContains(page, batch.payload[0]["password_hash"])
        self.assertEqual(page["Cache-Control"], "private, no-store")
        for row in batch.payload:
            self.assertNotIn("password", row)
        response = self.client.post(reverse("account_preview", args=[batch.pk]), {"action": "confirm"})
        self.assertRedirects(response, reverse("account_list"))
        for username, password in [("00000003", "Initial!Maple2026"), ("00000004", "Initial!Cedar2026")]:
            user = User.objects.get(username=username)
            self.assertTrue(user.check_password(password))
            self.assertFalse(user.is_staff)
            self.assertFalse(user.is_superuser)
            self.assertTrue(user.is_active)
        self.assertTrue(self.client.login(username="00000003", password="Initial!Maple2026"))
        self.assertRedirects(self.client.get(reverse('dashboard')), reverse('password_change'))
        self.assertRedirects(self.client.post(reverse('password_change'), {'old_password': 'Initial!Maple2026',
            'new_password1': 'Personal!Maple9842', 'new_password2': 'Personal!Maple9842'}), reverse('password_change_done'))
        self.assertContains(self.client.get(reverse("dashboard")), "新学生甲")
        self.assertEqual(self.client.get(reverse("account_list")).status_code, 403)
        batch.refresh_from_db()
        self.assertTrue(batch.is_confirmed)
        self.assertIsNotNone(batch.confirmed_at)
        self.assertEqual(batch.payload, [])
        self.assertTrue(Audit.objects.filter(action="批量创建学生账号：2 人").exists())

    def test_template_has_bom_and_exact_headers_only(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("account_template"))
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")))), [STUDENT_IMPORT_HEADERS])
        self.assertIn("attachment", response["Content-Disposition"])

    def test_stream_reports_only_completed_counts_until_preview_is_saved(self):
        self.client.force_login(self.admin)
        students = [[f"stream-{i}", "学生", "", "Initial!Maple2026"] for i in range(40)]
        response = self.client.post(reverse("account_upload"), {"mode": "create", "file": self.upload_file(students)},
                                    HTTP_ACCEPT="application/x-ndjson")
        self.assertEqual(response["X-Accel-Buffering"], "no")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        events = iter(response.streaming_content)
        self.assertEqual(json.loads(next(events)), {"completed": 0, "total": len(students)})
        completed = 0
        while completed < len(students):
            event = json.loads(next(events))
            self.assertGreater(event["completed"], completed)
            self.assertLess(event["completed"] - completed, len(students))
            completed = event["completed"]
            self.assertEqual(event, {"completed": completed, "total": len(students)})
        self.assertFalse(StudentImportBatch.objects.exists())
        final = json.loads(next(events))
        self.assertEqual(list(events), [])
        batch = StudentImportBatch.objects.get(uploaded_by=self.admin)
        self.assertEqual(final, {"preview": reverse("account_preview", args=[batch.pk])})
        self.assertFalse(User.objects.filter(username__startswith="stream-").exists())
        self.assertEqual([row["username"] for row in batch.payload], [item[0] for item in students])
        self.assertTrue(all("password_hash" in row and "password" not in row for row in batch.payload))
        self.client.post(final["preview"], {"action": "confirm"})
        self.assertEqual(User.objects.filter(username__startswith="stream-").count(), len(students))

    def test_invalid_stream_and_disconnect_do_not_save_partial_previews(self):
        self.client.force_login(self.admin)
        url = reverse("account_upload")
        response = self.client.post(url, {"file": self.upload_file([["invalid", "学生", "", "123"]])},
                                    HTTP_ACCEPT="application/x-ndjson")
        self.assertEqual(response.status_code, 400)
        self.assertIn("第 2 行", " ".join(response.json()["errors"]))
        response = self.client.post(url, {"file": self.upload_file()}, HTTP_ACCEPT="application/x-ndjson")
        events = iter(response.streaming_content)
        next(events)
        next(events)
        response.close()
        self.assertFalse(StudentImportBatch.objects.exists())
        self.assertFalse(User.objects.filter(username="00000003").exists())

    def test_permissions_and_preview_ownership(self):
        batch = self.create_preview()
        urls = [reverse("account_upload"), reverse("account_template"), reverse("account_preview", args=[batch.pk])]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.student)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, {"action": "confirm"}).status_code, 403)
        self.client.force_login(self.other_admin)
        self.assertEqual(self.client.get(urls[-1]).status_code, 404)
        self.assertEqual(self.client.post(urls[-1], {"action": "confirm"}).status_code, 404)
        self.assertNotContains(self.client.get(reverse("account_list")), urls[-1])
        with self.assertRaises(ValidationError):
            confirm_student_accounts(batch.pk, self.student)
        self.assertFalse(User.objects.filter(username="00000003").exists())

    def test_cancel_creates_no_accounts(self):
        batch = self.create_preview()
        self.client.force_login(self.admin)
        response = self.client.post(reverse("account_preview", args=[batch.pk]), {"action": "cancel"})
        self.assertRedirects(response, reverse("account_list"))
        self.assertFalse(StudentImportBatch.objects.filter(pk=batch.pk).exists())
        self.assertFalse(User.objects.filter(username="00000003").exists())

    def test_confirmation_replay_and_cancel_after_commit(self):
        batch = self.create_preview()
        confirm_student_accounts(batch.pk, self.admin)
        before = User.objects.count()
        self.client.force_login(self.admin)
        url = reverse("account_preview", args=[batch.pk])
        self.assertContains(self.client.get(url), "导入完成")
        self.assertContains(self.client.post(url, {"action": "confirm"}, follow=True), "请勿重复确认")
        self.assertContains(self.client.post(url, {"action": "cancel"}, follow=True), "不能取消")
        self.assertEqual(User.objects.count(), before)
        self.assertEqual(Audit.objects.filter(action="批量创建学生账号：2 人").count(), 1)
        self.assertTrue(StudentImportBatch.objects.get(pk=batch.pk).is_confirmed)

    def test_existing_accounts_fail_whole_file_without_overwriting(self):
        original = self.student.password
        self.client.force_login(self.admin)
        upload = self.upload_file([["00000003", "新学生", "", "Initial!Maple2026"], [self.student.username, "覆盖姓名", "", "Changed!Cedar2026"]])
        self.assertContains(self.client.post(reverse("account_upload"), {"file": upload}), "第 3 行：该学号已存在")
        self.assertEqual(StudentImportBatch.objects.count(), 0)
        self.assertFalse(User.objects.filter(username="00000003").exists())
        self.student.refresh_from_db()
        self.assertEqual(self.student.password, original)
        self.assertEqual(self.student.full_name, "已有学生")

    def test_csv_duplicate_and_normalized_duplicate_rejected(self):
        for usernames in [("00000003", "00000003"), ("StudentA", "studenta"), ("00000003", "０００００００３")]:
            with self.subTest(usernames=usernames), self.assertRaisesMessage(ValidationError, "重复"):
                parse_student_accounts(self.upload_file([[name, "新学生", "", "Initial!Maple2026"] for name in usernames]))
        with self.assertRaisesMessage(ValidationError, "已存在"):
            parse_student_accounts(self.upload_file([["MANAGER", "新学生", "", "Initial!Maple2026"]]))

    def test_invalid_rows_reject_without_persisting_passwords(self):
        invalid = [
            ["", "姓名", "", "Initial!Maple2026"],
            ["bad user", "姓名", "", "Initial!Maple2026"],
            ["00000003", "", "", "Initial!Maple2026"],
            ["00000003", "姓名", "", "123456"],
            ["00000003", "姓名", "", "1234567890123"],
            ["00000003", "姓名", "", ""],
            ["00000003", "姓名", "", "a" * 129],
            ["00000003", "姓名", "系" * 101, "Initial!Maple2026"],
            ["00000003", "姓名"],
            ["00000003", "姓名", "", "Initial!Maple2026", "extra"],
        ]
        for row in invalid:
            with self.subTest(row=row[:3]), self.assertRaises(ValidationError):
                parse_student_accounts(self.upload_file([row]))
        self.assertFalse(User.objects.filter(username="00000003").exists())

    def test_csv_encoding_headers_empty_and_malformed_rejected(self):
        uploads = [self.upload_file([]), self.upload_file(header=["学号", "姓名", "院系/班级", "初始密码", "is_staff"]),
                   SimpleUploadedFile("bad.csv", b"\xff\xfe"), SimpleUploadedFile("bad.csv", b"\x00"),
                   SimpleUploadedFile("bad.csv", '学号,姓名,院系/班级,初始密码\n00000003,张三,学院,"unclosed'.encode())]
        for upload in uploads:
            with self.subTest(name=upload.name), self.assertRaises(ValidationError):
                parse_student_accounts(upload)

    def test_file_size_extension_and_row_limit(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("account_upload"), {"file": SimpleUploadedFile("wrong.xlsx", b"content")})
        self.assertContains(response, "CSV 文件")
        oversized = SimpleUploadedFile("students.csv", b"x" * (STUDENT_IMPORT_MAX_BYTES + 1))
        self.assertContains(self.client.post(reverse("account_upload"), {"file": oversized}), "5 MB")
        rows = [[str(30000000 + index), "新学生", "", "Initial!Maple2026"] for index in range(STUDENT_IMPORT_MAX_ROWS + 1)]
        with self.assertRaisesMessage(ValidationError, "分批导入"):
            parse_student_accounts(self.upload_file(rows))

    def test_account_created_after_preview_aborts_all_rows(self):
        batch = self.create_preview()
        existing = User.objects.create(username="00000004", full_name="其他管理员创建", password=make_password("Other!initial2026"))
        with self.assertRaisesMessage(ValidationError, "本次未导入任何账号"):
            confirm_student_accounts(batch.pk, self.admin)
        self.assertFalse(User.objects.filter(username="00000003").exists())
        self.assertEqual(User.objects.get(pk=existing.pk).full_name, "其他管理员创建")
        batch.refresh_from_db()
        self.assertFalse(batch.is_confirmed)
        self.assertEqual(len(batch.payload), 2)

    def test_transaction_rolls_back_users_and_confirmation_on_failure(self):
        batch = self.create_preview()
        with patch("portal.services.audit", side_effect=IntegrityError("simulated conflict")):
            with self.assertRaisesMessage(ValidationError, "账号发生冲突"):
                confirm_student_accounts(batch.pk, self.admin)
        self.assertFalse(User.objects.filter(username__in=["00000003", "00000004"]).exists())
        batch.refresh_from_db()
        self.assertFalse(batch.is_confirmed)
        self.assertIsNone(batch.confirmed_at)
        self.assertEqual(len(batch.payload), 2)

    def test_expired_preview_cannot_be_confirmed(self):
        batch = self.create_preview()
        StudentImportBatch.objects.filter(pk=batch.pk).update(created_at=timezone.now() - timedelta(days=2))
        with self.assertRaisesMessage(ValidationError, "超过一天"):
            confirm_student_accounts(batch.pk, self.admin)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("account_preview", args=[batch.pk]))
        self.assertContains(response, "不能继续确认")
        self.assertNotContains(response, 'value="confirm"')
        self.assertFalse(User.objects.filter(username="00000003").exists())

    def test_get_missing_action_and_csrf_do_not_create_accounts(self):
        batch = self.create_preview()
        self.client.force_login(self.admin)
        url = reverse("account_preview", args=[batch.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertContains(self.client.post(url, {}, follow=True), "请选择确认导入或取消导入")
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.admin)
        self.assertEqual(protected.post(url, {"action": "confirm"}).status_code, 403)
        self.assertFalse(User.objects.filter(username="00000003").exists())

    def test_cleanup_removes_only_expired_pending_account_previews(self):
        expired = self.create_preview()
        fresh = self.create_preview()
        confirmed = StudentImportBatch.objects.create(uploaded_by=self.admin, row_count=2, is_confirmed=True)
        StudentImportBatch.objects.filter(pk__in=[expired.pk, confirmed.pk]).update(created_at=timezone.now() - timedelta(days=2))
        call_command("cleanup_data", stdout=io.StringIO())
        self.assertFalse(StudentImportBatch.objects.filter(pk=expired.pk).exists())
        self.assertTrue(StudentImportBatch.objects.filter(pk=fresh.pk).exists())
        self.assertTrue(StudentImportBatch.objects.filter(pk=confirmed.pk).exists())

    def test_import_never_accepts_privilege_fields_from_payload(self):
        batch = self.create_preview()
        for row in batch.payload:
            row.update(is_staff=True, is_superuser=True)
        batch.save()
        confirm_student_accounts(batch.pk, self.admin)
        self.assertFalse(User.objects.filter(username__in=["00000003", "00000004"], is_staff=True).exists())
        self.assertFalse(User.objects.filter(username__in=["00000003", "00000004"], is_superuser=True).exists())

    @override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.PBKDF2PasswordHasher"])
    def test_real_password_hashes_are_individually_salted(self):
        password = "Initial!Maple2026"
        rows = parse_student_accounts(self.upload_file([["00000003", "学生甲", "", password], ["00000004", "学生乙", "", password]]))
        self.assertTrue(all(row["password_hash"].startswith("pbkdf2_sha256$") for row in rows))
        self.assertNotEqual(rows[0]["password_hash"], rows[1]["password_hash"])
        batch = StudentImportBatch.objects.create(uploaded_by=self.admin, row_count=2, payload=rows)
        confirm_student_accounts(batch.pk, self.admin)
        self.assertTrue(User.objects.get(username="00000003").check_password(password))

    def test_initial_password_whitespace_is_preserved(self):
        password = " Initial!Maple2026 "
        batch = self.create_preview(rows=[["00000003", "新学生", "", password]])
        confirm_student_accounts(batch.pk, self.admin)
        user = User.objects.get(username="00000003")
        self.assertTrue(user.check_password(password))
        self.assertFalse(user.check_password(password.strip()))

    def test_1500_rows_validate_with_one_account_lookup(self):
        rows = [[str(70000000 + index), f"学生{index}", "计算机学院", "Initial!Maple2026"] for index in range(1500)]
        with self.assertNumQueries(1):
            payload = parse_student_accounts(self.upload_file(rows))
        self.assertEqual(len(payload), 1500)
        self.assertEqual(payload[0]["username"], "70000000")
        self.assertEqual(payload[-1]["username"], "70001499")
        self.assertNotIn("password", payload[-1])

    def test_1500_person_preview_paginates_and_confirmation_creates_all(self):
        rows = [[str(71000000 + index), f"学生{index}", "计算机学院", "Initial!Maple2026"] for index in range(1500)]
        batch = self.create_preview(rows=rows)
        self.client.force_login(self.admin)
        url = reverse("account_preview", args=[batch.pk])
        first_page = self.client.get(url)
        self.assertEqual(first_page.context["page_obj"].paginator.count, 1500)
        self.assertEqual(first_page.context["page_obj"].paginator.num_pages, 15)
        self.assertEqual(len(first_page.context["rows"]), 100)
        self.assertContains(first_page, "71000000")
        self.assertNotContains(first_page, "71000100")
        second_page = self.client.get(url + "?page=2")
        self.assertContains(second_page, "71000100")
        self.assertNotContains(second_page, "71000000")
        self.assertContains(second_page, "确认创建 1500 个学生账号")
        response = self.client.post(url + "?page=2", {"action": "confirm"})
        self.assertRedirects(response, reverse("account_list"))
        self.assertEqual(User.objects.filter(username__startswith="7100").count(), 1500)
        self.assertTrue(User.objects.get(username="71001499").check_password("Initial!Maple2026"))
        batch.refresh_from_db()
        self.assertEqual(batch.payload, [])
        self.assertTrue(batch.is_confirmed)

    def test_exact_2000_row_limit_is_accepted(self):
        self.assertEqual(STUDENT_IMPORT_MAX_ROWS, 2000)
        rows = [[str(72000000 + index), "新学生", "", "Initial!Maple2026"] for index in range(2000)]
        self.assertEqual(len(parse_student_accounts(self.upload_file(rows))), 2000)

    def test_invalid_last_row_in_1500_does_not_hash_or_create_any_accounts(self):
        rows = [[str(73000000 + index), "新学生", "", "Initial!Maple2026"] for index in range(1500)]
        rows[-1][-1] = "123456"
        self.client.force_login(self.admin)
        with patch("portal.services.make_password") as hasher:
            response = self.client.post(reverse("account_upload"), {"file": self.upload_file(rows)})
        self.assertContains(response, "第 1501 行")
        hasher.assert_not_called()
        self.assertEqual(StudentImportBatch.objects.count(), 0)
        self.assertFalse(User.objects.filter(username__startswith="7300").exists())

    def test_student_upload_allows_over_two_megabytes_but_limits_five(self):
        accepted = SimpleUploadedFile("students.csv", b"x" * (2 * 1024 * 1024 + 1))
        self.assertTrue(StudentImportForm(files={"file": accepted}).is_valid())
        oversized = SimpleUploadedFile("students.csv", b"x" * (5 * 1024 * 1024 + 1))
        self.assertFalse(StudentImportForm(files={"file": oversized}).is_valid())
