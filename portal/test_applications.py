import csv
import io
import re
import tempfile
from unittest.mock import patch
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from .models import Leave, Registration, RegistrationAttachment, RegistrationEvent, User


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ApplicationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(username="manager", full_name="管理员", is_staff=True)
        cls.student = User.objects.create_user(username="001234", full_name="张同学")
        cls.other = User.objects.create_user(username="005678", full_name="李同学")

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        setting = override_settings(MEDIA_ROOT=folder.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.client.force_login(self.student)

    def upload(self, name="申请表.pdf", body=b"%PDF-1.4 test"):
        return SimpleUploadedFile(name, body)

    def data(self, kind="internship", **extra):
        return {"starts_at": "2026-11-10T08:00", "ends_at": "2026-12-10T18:00", "employer": "研究院",
                "position": "研发实习生", "address": "浙江省杭州市西湖区示例路1号2栋301室", "contact": "13800000000",
                "emergency_name": "张家长", "emergency_contact": "13900000000", "rental_reason": "research",
                "has_dorm": "no", "attachments": self.upload(), **extra}

    def create(self, kind="internship", **extra):
        response = self.client.post(reverse(f"{kind}_create"), self.data(kind, **extra))
        self.assertEqual(response.status_code, 302, response.content.decode())
        return Registration.objects.latest("pk")

    def test_navigation_module_after_guides_and_before_support(self):
        for user in [self.admin, self.student]:
            self.client.force_login(user)
            response = self.client.get(reverse("dashboard"))
            nav = response.content.decode().split('<nav aria-label="主导航">')[1].split('</nav>')[0]
            links = re.findall(r'href="([^"]+)"', nav)
            self.assertEqual(links[:8], ["/", "/notifications/", "/guides/", "/leaves/",
                                         "/internships/", "/rentals/", "/faqs/", "/consultations/"])
            self.assertContains(response, 'aria-label="请销假、实习和租房"')
        self.assertRedirects(self.client.get(reverse("applications_home")), reverse("leave_list"))

    def test_both_application_forms_show_template_link_and_account_identity(self):
        for kind in Registration.Kind.values:
            response = self.client.get(reverse(f"{kind}_create"))
            self.assertContains(response, 'href="/guides/"')
            self.assertContains(response, "寻找申请模板")
            self.assertEqual(response.context["form"]["account_name"].value(), self.student.full_name)
            self.assertEqual(response.context["form"]["account_number"].value(), self.student.username)

    def test_internship_creation_saves_identity_required_information_files_event(self):
        item = self.create(account_name="冒名", account_number="fake", student=self.other.pk, status="approved")
        self.assertEqual((item.student_id, item.student_name, item.student_number),
                         (self.student.pk, self.student.full_name, self.student.username))
        self.assertEqual((item.status, item.kind, item.employer, item.position), ("pending", "internship", "研究院", "研发实习生"))
        self.assertEqual(item.attachments.get().original_name, "申请表.pdf")
        self.assertEqual(item.events.get().actor_id, self.student.pk)
        self.assertEqual(item.events.get().status, "pending")

    def test_internship_all_required_fields_checked(self):
        for field in ["starts_at", "ends_at", "employer", "position", "address", "contact", "emergency_name", "emergency_contact", "attachments"]:
            data = self.data()
            data.pop(field)
            response = self.client.post(reverse("internship_create"), data)
            self.assertIn(field, response.context["form"].errors)
        self.assertFalse(Registration.objects.exists())

    def test_rental_required_fields_checked(self):
        for field in ["starts_at", "ends_at", "rental_reason", "address", "contact", "emergency_name", "emergency_contact", "has_dorm", "attachments"]:
            data = self.data("rental")
            data.pop(field)
            response = self.client.post(reverse("rental_create"), data)
            self.assertIn(field, response.context["form"].errors)
        self.assertFalse(Registration.objects.exists())

    def test_other_rental_reason_requires_explanation(self):
        response = self.client.post(reverse("rental_create"), self.data("rental", rental_reason="other"))
        self.assertContains(response, "请说明具体理由")
        self.assertFalse(Registration.objects.exists())
        item = self.create("rental", rental_reason="other", reason_detail="家庭照护安排")
        self.assertEqual(item.reason_detail, "家庭照护安排")

    def test_dorm_selection_required_and_dorm_number_conditional(self):
        response = self.client.post(reverse("rental_create"), self.data("rental", has_dorm="yes"))
        self.assertContains(response, "请填写校内宿舍号")
        item = self.create("rental", has_dorm="yes", dorm_number="东区3栋201")
        self.assertTrue(item.has_dorm)
        self.assertContains(self.client.get(reverse("rental_detail", args=[item.pk])), "东区3栋201")
        item = self.create("rental", has_dorm="no", dorm_number="无效旧输入")
        self.assertFalse(item.has_dorm)
        self.assertEqual(item.dorm_number, "")

    def test_dates_must_increase_for_both_kinds(self):
        for kind in Registration.Kind.values:
            response = self.client.post(reverse(f"{kind}_create"), self.data(kind, ends_at="2026-11-10T08:00"))
            self.assertContains(response, "结束时间必须晚于开始时间")
        self.assertFalse(Registration.objects.exists())

    def test_invalid_choices_are_rejected(self):
        for extra in [{"rental_reason": "unexpected"}, {"has_dorm": "unexpected"}]:
            response = self.client.post(reverse("rental_create"), self.data("rental", **extra))
            self.assertTrue(response.context["form"].errors)
        self.assertFalse(Registration.objects.exists())

    def test_png_jpeg_and_pdf_multiple_upload_and_download(self):
        files = [self.upload(), self.upload("签字.png", b"\x89PNG\r\n\x1a\nimage"), self.upload("材料.jpg", b"\xff\xd8\xffimage")]
        item = self.create(attachments=files)
        self.assertEqual(item.attachments.count(), 3)
        for attachment in item.attachments.all():
            response = self.client.get(reverse("internship_file", args=[item.pk, attachment.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertIn("attachment", response["Content-Disposition"])
            b"".join(response.streaming_content)

    def test_unsupported_or_disguised_files_rejected(self):
        for name, body in [("script.html", b"<script>bad</script>"), ("fake.png", b"not png"), ("fake.pdf", b"not pdf"), ("fake.docx", b"not office")]:
            response = self.client.post(reverse("internship_create"), self.data(attachments=self.upload(name, body)))
            self.assertIn("attachments", response.context["form"].errors)
        self.assertFalse(RegistrationAttachment.objects.exists())

    def test_attachment_count_and_size_limits(self):
        response = self.client.post(reverse("rental_create"), self.data("rental", attachments=[self.upload(f"{i}.pdf") for i in range(11)]))
        self.assertContains(response, "最多上传 10 个附件")
        response = self.client.post(reverse("rental_create"), self.data("rental", attachments=self.upload(body=b"%PDF-" + b"x" * (20 * 1024 * 1024))))
        self.assertContains(response, "不能超过 20 MB")
        self.assertFalse(Registration.objects.exists())

    def test_missing_account_name_blocks_submission(self):
        self.student.full_name = ""
        self.student.save()
        response = self.client.post(reverse("internship_create"), self.data())
        self.assertIn("account_name", response.context["form"].errors)
        self.assertFalse(Registration.objects.exists())

    def test_ownership_and_cross_module_attachment_isolation(self):
        item = self.create()
        second = self.create()
        attachment = item.attachments.get()
        self.assertEqual(self.client.get(reverse("internship_file", args=[second.pk, attachment.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("rental_detail", args=[item.pk])).status_code, 404)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("internship_detail", args=[item.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("internship_file", args=[item.pk, attachment.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("internship_list")), "研究院")

    def test_review_permissions_and_csrf(self):
        item = self.create()
        url = reverse("internship_review", args=[item.pk])
        self.assertEqual(self.client.post(url, {"action": "approve"}).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.admin)
        self.assertEqual(strict.post(url, {"action": "approve"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("internship_create"), self.data()).status_code, 403)

    def test_rejection_requires_reason_and_student_reads_it(self):
        item = self.create("rental")
        self.client.force_login(self.admin)
        url = reverse("rental_review", args=[item.pk])
        self.assertContains(self.client.post(url, {"action": "reject", "note": "  "}), "驳回时必须填写驳回原因", status_code=400)
        item.refresh_from_db()
        self.assertEqual(item.status, "pending")
        self.assertEqual(item.events.count(), 1)
        self.client.post(url, {"action": "reject", "note": "缺少导师签字，请补充材料"})
        item.refresh_from_db()
        self.assertEqual(item.status, "rejected")
        self.client.force_login(self.student)
        self.assertContains(self.client.get(reverse("rental_detail", args=[item.pk])), "缺少导师签字，请补充材料")

    def test_review_is_single_transition_and_never_overwritten(self):
        item = self.create()
        self.client.force_login(self.admin)
        url = reverse("internship_review", args=[item.pk])
        self.client.post(url, {"action": "approve", "note": "同意实习"})
        self.client.post(url, {"action": "reject", "note": "重复审核"})
        item.refresh_from_db()
        self.assertEqual(item.status, "approved")
        self.assertEqual(item.events.count(), 2)
        self.assertEqual(item.events.last().note, "同意实习")

    def test_review_rejects_invalid_action_or_long_reason(self):
        item = self.create()
        self.client.force_login(self.admin)
        for data in [{"action": "delete"}, {"action": "reject", "note": "x" * 2001}]:
            self.assertEqual(self.client.post(reverse("internship_review", args=[item.pk]), data).status_code, 400)
        item.refresh_from_db()
        self.assertEqual(item.status, "pending")

    def test_list_combines_date_status_and_keyword_and_preserves_pagination(self):
        self.create("rental")
        for index in range(14):
            self.create(employer=f"关键词研究院{index}")
        response = self.client.get(reverse("internship_list"), {"q": "关键词", "start_date": "2026-11-20", "end_date": "2026-11-21", "status": "pending", "page": 2})
        self.assertEqual(response.context["page_obj"].paginator.count, 14)
        self.assertEqual(len(response.context["page_obj"]), 2)
        self.assertIn("start_date=2026-11-20", response.context["querystring"])
        self.assertEqual(self.client.get(reverse("internship_list"), {"end_date": "2026-11-01"}).context["page_obj"].paginator.count, 0)
        self.assertEqual(self.client.get(reverse("internship_list"), {"start_date": "bad"}).context["page_obj"].paginator.count, 0)

    def test_files_removed_if_submission_transaction_fails(self):
        from django.core.files.storage import default_storage
        with patch("portal.registration_views.RegistrationEvent.objects.create", side_effect=RuntimeError("test rollback")):
            with self.assertRaises(RuntimeError):
                self.create()
        self.assertFalse(Registration.objects.exists())
        self.assertFalse(RegistrationAttachment.objects.exists())
        self.assertEqual(default_storage.listdir("registrationattachment")[1], [])

    def leave_data(self, **extra):
        return {"kind": "personal", "reason": "办理事务", "starts_at": "2026-11-10T08:00", "ends_at": "2026-11-11T18:00",
                "province": "浙江省", "city": "杭州市", "contact": "13800000000", "tutor_signature": "no",
                "attachments": self.upload(), **extra}

    def test_leave_accepts_arbitrary_formats_and_downloads_only_with_permission(self):
        files = [self.upload("情况说明.html", b"<p>evidence</p>"), self.upload("report", b"plain file")]
        url = reverse("leave_create")
        field = self.client.get(url).context["form"]["attachments"]
        self.assertNotIn("accept=", str(field))
        self.assertIn("required", str(field))
        self.assertEqual(self.client.post(url, self.leave_data(attachments=files)).status_code, 302)
        item = Leave.objects.get()
        attachment, other_file = item.attachments.all()
        self.assertEqual(other_file.original_name, "report")
        download = reverse("leave_file", args=[item.pk, attachment.pk])
        for user in [self.student, self.admin]:
            self.client.force_login(user)
            self.assertContains(self.client.get(reverse("leave_detail", args=[item.pk])), download)
            response = self.client.get(download)
            self.assertEqual(response["Content-Type"], "application/octet-stream")
            self.assertEqual(response["X-Content-Type-Options"], "nosniff")
            self.assertTrue(response["Content-Disposition"].startswith("attachment;"))
            self.assertEqual(b"".join(response.streaming_content), b"<p>evidence</p>")
        with self.settings(USE_X_ACCEL_REDIRECT=True):
            self.assertRegex(self.client.get(download)["X-Accel-Redirect"], r"^/_protected/leaveattachment/[0-9a-f]{32}$")
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(download).status_code, 404)
        self.assertEqual(self.client.post(url, self.leave_data()).status_code, 302)
        other_leave = Leave.objects.get(student=self.other)
        self.assertEqual(self.client.get(reverse("leave_file", args=[other_leave.pk, attachment.pk])).status_code, 404)
        self.client.logout()
        self.assertRedirects(self.client.get(download), reverse("login") + "?next=" + download)

    def test_invalid_leave_attachments_block_the_entire_submission(self):
        invalid_batches = [
            [self.upload(), self.upload("empty.txt", b"")],
            [self.upload() for _ in range(11)],
            [self.upload("large.bin", b"x" * (20 * 1024 * 1024 + 1))],
        ]
        for files in invalid_batches:
            response = self.client.post(reverse("leave_create"), self.leave_data(attachments=files))
            self.assertIn("attachments", response.context["form"].errors)
        self.assertFalse(Leave.objects.exists())

    def test_failed_leave_submission_cleans_up_uploaded_files(self):
        from django.core.files.storage import default_storage
        with patch("portal.views.audit", side_effect=RuntimeError("audit failed")):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse("leave_create"), self.leave_data(attachments=[self.upload(), self.upload()]))
        self.assertFalse(Leave.objects.exists())
        self.assertEqual(default_storage.listdir("leaveattachment")[1], [])

    def test_leave_required_fields_including_province_city_tutor_signature(self):
        for field in self.leave_data():
            data = self.leave_data()
            data.pop(field)
            response = self.client.post(reverse("leave_create"), data)
            self.assertIn(field, response.context["form"].errors)
        self.assertFalse(Leave.objects.exists())

    def test_tutor_signature_unselected_and_accepts_only_explicit_yes_or_no(self):
        response = self.client.get(reverse("leave_create"))
        self.assertFalse(response.context["form"]["tutor_signature"].value())
        for value in ["", "maybe"]:
            self.assertIn("tutor_signature", self.client.post(reverse("leave_create"), self.leave_data(tutor_signature=value)).context["form"].errors)
        for value in ["yes", "no"]:
            self.assertEqual(self.client.post(reverse("leave_create"), self.leave_data(tutor_signature=value)).status_code, 302)
        self.assertEqual(Leave.objects.count(), 2)

    def test_leave_identity_and_tutor_choice_survive_account_change_and_export(self):
        self.client.post(reverse("leave_create"), self.leave_data(account_name="假姓名", account_number="fake"))
        item = Leave.objects.get()
        self.assertEqual(item.student_name, "张同学")
        self.assertEqual(item.destination, "浙江省 杭州市")
        self.student.full_name = "新姓名"
        self.student.save()
        self.assertContains(self.client.get(reverse("leave_detail", args=[item.pk])), "张同学")
        self.client.force_login(self.admin)
        response = self.client.get(reverse("leave_export"), {"start_date": "2026-11-10", "end_date": "2026-11-11"})
        rows = list(csv.DictReader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))
        self.assertEqual(rows[0]["姓名"], "张同学")
        self.assertEqual(rows[0]["学号"], "001234")
        self.assertEqual(rows[0]["去往城市"], "杭州市")
        self.assertEqual(rows[0]["是否导师本人线下签字"], "否：使用电子签名或代签，已征得导师同意")

    def test_anonymous_cannot_access_any_application_route(self):
        item = self.create()
        attachment = item.attachments.get()
        self.client.logout()
        for name, args in [("applications_home", []), ("internship_list", []), ("rental_list", []), ("internship_create", []),
                           ("rental_create", []), ("internship_detail", [item.pk]), ("internship_file", [item.pk, attachment.pk])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 302)
