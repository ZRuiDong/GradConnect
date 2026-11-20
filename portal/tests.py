import tempfile
from datetime import date, timedelta
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from .forms import PolicyForm, VideoForm
from .models import (MonthlyAttendance, AttendanceBatch, Conversation, FAQ, Leave, LeaveEvent, Matter, ServiceGuide,
                     Message, Policy, PolicyRevision, Publication, User, Video)
from .services import confirm_attendance, parse_attendance, transition_leave


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PortalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        password = make_password("Tests!safe2026")
        cls.admin = User.objects.create(username="manager", full_name="管理员", is_staff=True, is_superuser=True, password=password)
        cls.student = User.objects.create(username="20260001", full_name="学生甲", password=password)
        cls.other = User.objects.create(username="20260002", full_name="学生乙", password=password)
        cls.policy = Policy.objects.create(created_by=cls.admin, status="published")
        cls.revision = PolicyRevision.objects.create(policy=cls.policy, title="公开制度", body="公开正文", is_draft=False)
        cls.policy.published_revision = cls.revision
        cls.policy.save()
        cls.faq = FAQ.objects.create(question="公开问题", answer="官方答案", created_by=cls.admin, status="published")
        cls.matter = Matter.objects.create(title="公开事项", description="事项说明", status="published", created_by=cls.admin)
        cls.guide = ServiceGuide.objects.create(title="办事指南", category="daily", owner="管理员", contact="12345678",
            office="学院办公室", process="提交材料后等待办理。", created_by=cls.admin)

    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=self.media.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.media.cleanup)

    def login_as(self, user=None):
        self.client.force_login(user or self.student)

    def create_leave(self, **values):
        return Leave.objects.create(student=values.pop("student", self.student), kind="personal", starts_at=timezone.now(), ends_at=timezone.now() + timedelta(days=1), reason="事由", destination="学校", contact="13800000000", **values)

    def create_video(self):
        return Video.objects.create(title="学习视频", uploaded_by=self.admin, file=SimpleUploadedFile("lesson.mp4", b"\x00\x00\x00\x18ftypisom" + b"0" * 100))

    def csv(self, content):
        return SimpleUploadedFile("attendance.csv", content.encode("utf-8-sig"))

    def test_anonymous_is_redirected(self):
        for name in ["dashboard", "policy_list", "leave_list", "faq_list", "video_list", "attendance_list", "guide_list", "consultation_list", "account_list"]:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 302)

    def test_login_and_role_dashboard(self):
        response = self.client.post(reverse("login"), {"username": self.student.username, "password": "Tests!safe2026"})
        self.assertRedirects(response, reverse("dashboard"))
        self.assertContains(self.client.get("/"), "学生甲")
        self.assertNotContains(self.client.get("/"), "学生账号</a>", html=False)

    def test_login_rate_limit_and_logout_post(self):
        for _ in range(5):
            self.client.post(reverse("login"), {"username": self.student.username, "password": "bad"})
        response = self.client.post(reverse("login"), {"username": self.student.username, "password": "Tests!safe2026"})
        self.assertContains(response, "15 分钟")
        self.login_as()
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)
        self.assertEqual(self.client.post(reverse("logout")).status_code, 302)

    def test_student_cannot_access_admin_pages(self):
        self.login_as()
        for name in ["policy_create", "faq_create", "video_create", "guide_create", "attendance_upload", "attendance_template", "account_list", "account_create", "audit_list"]:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 403)

    def test_all_student_pages_render(self):
        self.login_as()
        for name in ["dashboard", "policy_list", "leave_list", "leave_create", "faq_list", "consultation_list", "consultation_create", "video_list", "attendance_list", "guide_list", "password_change"]:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        for name, pk in [("policy_detail", self.policy.pk), ("faq_detail", self.faq.pk), ("guide_detail", self.guide.pk)]:
            self.assertEqual(self.client.get(reverse(name, args=[pk])).status_code, 200)

    def test_all_admin_pages_render(self):
        self.login_as(self.admin)
        for name in ["dashboard", "policy_create", "faq_create", "video_create", "guide_create", "attendance_upload", "account_list", "account_create", "audit_list"]:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        for name in ["account_edit", "account_reset"]:
            self.assertEqual(self.client.get(reverse(name, args=[self.student.pk])).status_code, 200)

    def test_policy_draft_preserves_published_version(self):
        self.login_as(self.admin)
        url = reverse("policy_edit", args=[self.policy.pk])
        self.assertEqual(self.client.post(url, {"title": "尚未公开的标题", "category": "other", "body": "保密草稿"}).status_code, 302)
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.published_revision_id, self.revision.pk)
        self.login_as()
        self.assertContains(self.client.get(reverse("policy_detail", args=[self.policy.pk])), "公开正文")
        self.assertNotContains(self.client.get(reverse("policy_list")), "尚未公开的标题")
        self.assertNotContains(self.client.get(reverse("policy_list") + "?q=保密草稿"), "公开制度")
        self.login_as(self.admin)
        self.client.post(reverse("policy_action", args=[self.policy.pk, "publish"]))
        self.login_as()
        self.assertContains(self.client.get(reverse("policy_detail", args=[self.policy.pk])), "保密草稿")

    def test_policy_draft_and_old_file_hidden(self):
        self.revision.attachment.save("policy.pdf", SimpleUploadedFile("policy.pdf", b"%PDF-1.4 test"))
        draft = PolicyRevision.objects.create(policy=self.policy, number=2, title="草稿", body="正文")
        draft.attachment.save("draft.pdf", SimpleUploadedFile("draft.pdf", b"%PDF-1.4 draft"))
        self.login_as()
        self.assertEqual(self.client.get(reverse("policy_file", args=[draft.pk])).status_code, 404)
        response = self.client.get(reverse("policy_file", args=[self.revision.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        response.close()
        self.login_as(self.admin)
        self.client.post(reverse("policy_action", args=[self.policy.pk, "publish"]))
        self.login_as()
        self.assertEqual(self.client.get(reverse("policy_file", args=[self.revision.pk])).status_code, 404)

    def test_withdrawn_policy_inaccessible(self):
        self.login_as(self.admin)
        self.client.post(reverse("policy_action", args=[self.policy.pk, "withdraw"]))
        self.login_as()
        self.assertEqual(self.client.get(reverse("policy_detail", args=[self.policy.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("policy_file", args=[self.revision.pk])).status_code, 404)

    def test_policy_attachment_signature_checked(self):
        form = PolicyForm(data={"title": "制度", "category": "other", "body": "正文"}, files={"attachment": SimpleUploadedFile("fake.pdf", b"<script>bad</script>")})
        self.assertFalse(form.is_valid())

    def test_leave_full_lifecycle_and_history(self):
        item = self.create_leave()
        transition_leave(item.pk, self.admin, "approve", "同意")
        transition_leave(item.pk, self.admin, "close", "已线下办理")
        item.refresh_from_db()
        self.assertEqual(item.status, Leave.Status.CLOSED)
        self.assertEqual(item.events.count(), 2)
        self.assertEqual(item.events.last().note, "已线下办理")

    def test_leave_cannot_withdraw_after_approval(self):
        item = self.create_leave()
        transition_leave(item.pk, self.admin, "approve")
        with self.assertRaises(ValidationError):
            transition_leave(item.pk, self.student, "withdraw")
        self.assertEqual(item.events.count(), 1)

    def test_leave_mutual_exclusion(self):
        item = self.create_leave()
        transition_leave(item.pk, self.student, "withdraw")
        with self.assertRaises(ValidationError):
            transition_leave(item.pk, self.admin, "approve")
        item.refresh_from_db()
        self.assertEqual(item.status, "withdrawn")

    def test_leave_ownership_and_privilege(self):
        item = self.create_leave()
        self.login_as(self.other)
        self.assertEqual(self.client.get(reverse("leave_detail", args=[item.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("leave_action", args=[item.pk, "withdraw"])).status_code, 404)
        with self.assertRaises(ValidationError):
            transition_leave(item.pk, self.student, "approve")
        with self.assertRaises(ValidationError):
            transition_leave(item.pk, self.admin, "reject", "")

    def test_leave_create_validation_and_resubmit(self):
        self.login_as()
        data = {"kind": "personal", "starts_at": "2026-11-20T10:00", "ends_at": "2026-11-20T09:00", "reason": "事由", "province": "浙江省", "city": "杭州市", "tutor_signature": "yes", "contact": "13800000000"}
        self.assertContains(self.client.post(reverse("leave_create"), data), "结束时间必须晚于开始时间")
        data["ends_at"] = "2026-11-21T10:00"
        data["attachments"] = SimpleUploadedFile("original.txt", b"original")
        self.client.post(reverse("leave_create"), data)
        original = Leave.objects.get()
        transition_leave(original.pk, self.admin, "reject", "请完善原因")
        resubmit_url = reverse("leave_create") + f"?from={original.pk}"
        del data["attachments"]
        self.assertContains(self.client.post(resubmit_url, data), "请至少上传一份请假佐证材料")
        self.assertEqual(Leave.objects.count(), 1)
        data["attachments"] = SimpleUploadedFile("revised.txt", b"revised")
        self.client.post(resubmit_url, data)
        self.assertEqual(Leave.objects.count(), 2)
        self.assertEqual(Leave.objects.first().previous_id, original.pk)
        self.assertEqual(original.attachments.get().original_name, "original.txt")
        self.assertEqual(Leave.objects.first().attachments.get().original_name, "revised.txt")
        self.assertEqual(original.events.count(), 2)

    def test_legacy_leave_without_attachments_can_still_be_reviewed(self):
        item = self.create_leave()
        self.login_as(self.admin)
        self.assertContains(self.client.get(reverse("leave_detail", args=[item.pk])), "此申请未上传佐证材料")
        self.client.post(reverse("leave_action", args=[item.pk, "approve"]))
        item.refresh_from_db()
        self.assertEqual(item.status, Leave.Status.APPROVED)

    def test_csrf_enforced_for_mutation(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post(reverse("policy_action", args=[self.policy.pk, "withdraw"])).status_code, 403)
        self.assertEqual(client.get(reverse("policy_action", args=[self.policy.pk, "withdraw"])).status_code, 405)

    def test_consultation_ownership_and_reply(self):
        self.login_as()
        self.client.post(reverse("consultation_create"), {"category": "daily", "subject": "私人问题", "body": "私人内容"})
        item = Conversation.objects.get()
        self.login_as(self.other)
        self.assertNotContains(self.client.get(reverse("consultation_list")), "私人问题")
        url = reverse("consultation_detail", args=[item.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {"body": "伪造回复"}).status_code, 404)
        self.login_as(self.admin)
        self.client.post(url, {"body": "管理员回复"})
        item.refresh_from_db()
        self.assertFalse(item.awaiting_reply)
        self.login_as()
        self.assertContains(self.client.get(url), "管理员回复")
        self.client.post(url, {"body": "补充问题"})
        item.refresh_from_db()
        self.assertTrue(item.awaiting_reply)

    def test_faq_requires_confirmation_and_can_cancel(self):
        self.login_as(self.admin)
        self.client.post(reverse("faq_create"), {"question": "未确认问题", "answer": "答案", "category": "other"})
        draft = FAQ.objects.get(question="未确认问题")
        self.login_as()
        self.assertEqual(self.client.get(reverse("faq_detail", args=[draft.pk])).status_code, 404)
        self.login_as(self.admin)
        self.client.post(reverse("faq_action", args=[draft.pk, "publish"]))
        self.login_as()
        self.assertContains(self.client.get(reverse("faq_detail", args=[draft.pk])), "答案")
        self.login_as(self.admin)
        draft.status = "draft"
        draft.save()
        self.client.post(reverse("faq_action", args=[draft.pk, "cancel"]))
        self.assertFalse(FAQ.objects.filter(pk=draft.pk).exists())

    def test_consultation_to_faq_only_publishes_curated_answer(self):
        item = Conversation.objects.create(student=self.student, subject="原始私人主题")
        Message.objects.create(conversation=item, author=self.student, body="私人身份证号")
        Message.objects.create(conversation=item, author=self.admin, body="通用回复")
        self.login_as(self.admin)
        response = self.client.get(reverse("faq_create") + f"?source={item.pk}")
        self.assertContains(response, "通用回复")
        self.assertNotContains(response, "私人身份证号")
        self.client.post(reverse("faq_create") + f"?source={item.pk}", {"question": "通用问题", "answer": "整理后的公开回答", "category": "other"})
        faq = FAQ.objects.get(source=item)
        self.client.post(reverse("faq_action", args=[faq.pk, "publish"]))
        self.login_as(self.other)
        self.assertContains(self.client.get(reverse("faq_detail", args=[faq.pk])), "整理后的公开回答")
        self.assertNotContains(self.client.get(reverse("faq_detail", args=[faq.pk])), "私人身份证号")

    def test_retired_matter_endpoints_are_unavailable(self):
        for user in [self.admin, self.student]:
            self.login_as(user)
            for url in ["/matters/", "/matters/new/", f"/matters/{self.matter.pk}/", f"/matters/{self.matter.pk}/edit/",
                        f"/matters/{self.matter.pk}/actions/publish/"]:
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url, {"title": "不应创建"}).status_code, 404)
            self.assertNotContains(self.client.get(reverse("dashboard")), "在办事项")
        self.login_as()
        self.assertEqual(self.client.get(reverse("consultation_create") + f"?matter={self.matter.pk}").status_code, 404)

    def test_video_range_and_deletion(self):
        item = self.create_video()
        self.login_as()
        url = reverse("video_file", args=[item.pk])
        response = self.client.get(url, HTTP_RANGE="bytes=4-7")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(b"".join(response.streaming_content), b"ftyp")
        self.assertEqual(self.client.get(url, HTTP_RANGE="bytes=9999-").status_code, 416)
        self.assertEqual(self.client.get(url, HTTP_RANGE="bytes=-0").status_code, 416)
        self.assertEqual(self.client.get(url, HTTP_RANGE="bytes=" + "9" * 5000 + "-").status_code, 416)
        self.login_as(self.admin)
        self.client.post(reverse("video_delete", args=[item.pk]))
        self.login_as()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(reverse("video_detail", args=[item.pk])).status_code, 404)

    def test_video_upload_signature(self):
        form = VideoForm(data={"title": "伪视频", "category": "other"}, files={"file": SimpleUploadedFile("fake.mp4", b"bad")})
        self.assertFalse(form.is_valid())

    def test_attendance_preview_confirm_and_idempotency(self):
        rows = parse_attendance(self.csv("学号,姓名,月份,打卡记录,请假天数\n20260001,学生甲,2026-11,20,2\n"))
        batch = AttendanceBatch.objects.create(month=date(2026, 11, 1), row_count=1, payload=rows, uploaded_by=self.admin)
        self.assertEqual(MonthlyAttendance.objects.count(), 0)
        confirm_attendance(batch.pk, self.admin)
        with self.assertRaises(ValidationError):
            confirm_attendance(batch.pk, self.admin)
        self.assertEqual(MonthlyAttendance.objects.count(), 1)
        batch.refresh_from_db()
        self.assertEqual(batch.payload, [])

    def test_attendance_invalid_csv_is_rejected(self):
        for body in ["学号,姓名,月份,打卡记录,请假天数\nmissing,学生甲,2026-11,20,2", "学号,姓名,月份,打卡记录,请假天数\n20260001,学生甲,2026-13,20,2", "学号,姓名,月份,打卡记录,请假天数\n20260001,学生甲,2026-11,未知,2", "学号,日期,状态,备注\n20260001,2026-11-01,正常,", "wrong,header"]:
            with self.subTest(body=body), self.assertRaises(ValidationError):
                parse_attendance(self.csv(body))

    def test_attendance_upload_preview_and_update_existing(self):
        self.login_as(self.admin)
        self.client.post(reverse("attendance_upload"), {"file": self.csv("学号,姓名,月份,打卡记录,请假天数\n20260001,学生甲,2026-11,20,2\n")})
        batch = AttendanceBatch.objects.get()
        self.assertContains(self.client.get(reverse("attendance_preview", args=[batch.pk])), "2026-11")
        self.client.post(reverse("attendance_preview", args=[batch.pk]), {"action": "confirm"})
        rows = parse_attendance(self.csv("学号,姓名,月份,打卡记录,请假天数\n20260001,学生甲,2026-11,21,3\n"))
        new = AttendanceBatch.objects.create(month=date(2026, 11, 1), row_count=1, payload=rows, uploaded_by=self.admin)
        confirm_attendance(new.pk, self.admin)
        self.assertEqual(MonthlyAttendance.objects.count(), 1)
        self.assertEqual(MonthlyAttendance.objects.get().total_days, 24)

    def test_student_only_sees_own_attendance(self):
        batch = AttendanceBatch.objects.create(month=date(2026, 11, 1), uploaded_by=self.admin, row_count=2, is_confirmed=True)
        MonthlyAttendance.objects.create(student=self.student, student_name="甲本人记录", student_number=self.student.username,
            month=date(2026, 11, 1), clock_days=20, leave_days=2, batch=batch)
        MonthlyAttendance.objects.create(student=self.other, student_name="乙私人记录", student_number=self.other.username,
            month=date(2026, 11, 1), clock_days=10, leave_days=1, batch=batch)
        self.login_as()
        response = self.client.get(reverse("attendance_list") + "?month=2026-11&student=20260002")
        self.assertContains(response, "甲本人记录")
        self.assertNotContains(response, "乙私人记录")
        self.assertEqual(self.client.get(reverse("attendance_preview", args=[batch.pk])).status_code, 403)

    def test_admin_reset_password_invalidates_old_session(self):
        self.login_as()
        old_cookie = self.client.cookies.copy()
        manager = Client()
        manager.force_login(self.admin)
        response = manager.post(reverse("account_reset", args=[self.student.pk]), {"new_password1": "Changed!safe2026", "new_password2": "Changed!safe2026"})
        self.assertEqual(response.status_code, 302)
        self.client.cookies = old_cookie
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)

    def test_deactivated_account_cannot_continue_session(self):
        self.login_as()
        self.student.is_active = False
        self.student.save()
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)

    def test_private_media_is_not_publicly_routed(self):
        self.assertEqual(self.client.get("/private-media/anything.pdf").status_code, 404)

    def test_health_and_xss_escaping(self):
        self.assertEqual(self.client.get(reverse("health")).json(), {"status": "ok"})
        self.revision.body = "<script>alert(1)</script>"
        self.revision.save()
        self.login_as()
        self.assertContains(self.client.get(reverse("policy_detail", args=[self.policy.pk])), "&lt;script&gt;")

    def test_invalid_related_ids_return_404(self):
        self.login_as()
        for url in [reverse("leave_create") + "?from=bad", reverse("consultation_create") + "?matter=bad"]:
            self.assertEqual(self.client.get(url).status_code, 404)
        self.login_as(self.admin)
        self.assertEqual(self.client.get(reverse("faq_create") + "?source=bad").status_code, 404)
        self.assertEqual(self.client.get(reverse("policy_detail", args=[self.policy.pk]) + "?version=bad").status_code, 404)

    def test_detail_pages_and_admin_forms_render(self):
        item = self.create_leave()
        LeaveEvent.objects.create(leave=item, actor=self.student, status="pending", note="提交申请")
        video = self.create_video()
        self.login_as(self.admin)
        for url in [reverse("leave_detail", args=[item.pk]), reverse("video_detail", args=[video.pk]), reverse("guide_detail", args=[self.guide.pk]), reverse("faq_detail", args=[self.faq.pk]), reverse("policy_detail", args=[self.policy.pk]), reverse("policy_edit", args=[self.policy.pk]), reverse("guide_edit", args=[self.guide.pk]), reverse("faq_edit", args=[self.faq.pk])]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        transition_leave(item.pk, self.admin, "approve")
        self.assertContains(self.client.get(reverse("leave_detail", args=[item.pk])), "确认已线下销假")
        self.login_as()
        self.assertContains(self.client.get(reverse("leave_detail", args=[item.pk])), "请在线下完成销假")

    def test_frame_protection_is_limited_to_pdf_endpoint(self):
        self.revision.attachment.save("policy.pdf", SimpleUploadedFile("policy.pdf", b"%PDF-1.4 test"))
        self.login_as()
        response = self.client.get(reverse("policy_file", args=[self.revision.pk]))
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
        response.close()
        self.assertEqual(self.client.get(reverse("dashboard"))["X-Frame-Options"], "DENY")

    def test_account_creation_cannot_grant_staff(self):
        self.login_as(self.admin)
        response = self.client.post(reverse("account_create"), {"username": "20260003", "full_name": "学生丙", "id_number": "110000199001010001", "enrollment_status": "enrolled", "party_branch": "无", "student_class": "2026级一班", "department": "计算机学院", "password1": "SafeTest!2026", "password2": "SafeTest!2026", "is_staff": "1", "is_superuser": "1"})
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username="20260003")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_import_cancel_and_permission(self):
        batch = AttendanceBatch.objects.create(month=date(2026, 11, 1), uploaded_by=self.admin, row_count=0)
        self.login_as(self.admin)
        self.client.post(reverse("attendance_preview", args=[batch.pk]), {"action": "cancel"})
        self.assertFalse(AttendanceBatch.objects.filter(pk=batch.pk).exists())
        with self.assertRaises(ValidationError):
            confirm_attendance(999, self.student)

    def test_x_accel_files_use_internal_location(self):
        video = self.create_video()
        self.login_as()
        with override_settings(USE_X_ACCEL_REDIRECT=True):
            response = self.client.get(reverse("video_file", args=[video.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response["X-Accel-Redirect"].startswith("/_protected/video/"))
            self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_cleanup_only_removes_old_unconfirmed_previews(self):
        from django.core.management import call_command
        batch = AttendanceBatch.objects.create(month=date(2026, 11, 1), uploaded_by=self.admin, row_count=0)
        AttendanceBatch.objects.filter(pk=batch.pk).update(created_at=timezone.now() - timedelta(days=2))
        kept = AttendanceBatch.objects.create(month=date(2026, 11, 1), uploaded_by=self.admin, row_count=0, is_confirmed=True)
        call_command("cleanup_data", verbosity=0)
        self.assertFalse(AttendanceBatch.objects.filter(pk=batch.pk).exists())
        self.assertTrue(AttendanceBatch.objects.filter(pk=kept.pk).exists())
