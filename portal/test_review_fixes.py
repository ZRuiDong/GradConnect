import tempfile
import csv
import io
from datetime import date
from html.parser import HTMLParser
from unittest.mock import patch
from django.contrib.auth.forms import SetPasswordForm
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from .models import CadreTenure, FAQ, Notification, Policy, PolicyRevision, ServiceGuide, GuideAttachment, StudentImportBatch, User, Video
from .services import confirm_student_accounts, parse_student_accounts
from .forms import STUDENT_IMPORT_HEADERS


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ReviewRegressionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(username="review-manager", full_name="管理员", is_staff=True)
        cls.student = User.objects.create_user(username="review-student", full_name="学生")

    def profile_data(self, token=""):
        return {"full_name": "表单中的姓名", "id_number": "110000199001010001", "enrollment_status": "enrolled",
            "party_branch": "无", "student_class": "表单班级", "political_status": "mass", "department": "学院",
            "is_active": "on", "edit_token": token}

    def edit_token(self):
        return self.client.get(reverse("account_edit", args=[self.student.pk])).context["edit_token"]

    def test_sidebar_native_groups_default_closed_and_active_module_opens(self):
        class Groups(HTMLParser):
            def __init__(self):
                super().__init__()
                self.groups = {}

            def handle_starttag(self, tag, attrs):
                values = dict(attrs)
                if tag == "details" and "data-nav-group" in values:
                    self.groups[values["data-nav-group"]] = "open" in values

        for user in [self.admin, self.student]:
            self.client.force_login(user)
            for name, expected in [("dashboard", {"applications": False, "support": False}),
                                   ("leave_list", {"applications": True, "support": False}),
                                   ("faq_list", {"applications": False, "support": True})]:
                parser = Groups()
                parser.feed(self.client.get(reverse(name)).content.decode())
                self.assertEqual(parser.groups, expected)

    def test_changed_student_profile_rejects_stale_edit_without_saving(self):
        self.client.force_login(self.admin)
        token = self.edit_token()
        User.objects.filter(pk=self.student.pk).update(full_name="另一管理员保存的姓名", student_class="新班级")
        response = self.client.post(reverse("account_edit", args=[self.student.pk]), self.profile_data(token))
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "重新加载最新资料", status_code=409)
        self.student.refresh_from_db()
        self.assertEqual((self.student.full_name, self.student.student_class), ("另一管理员保存的姓名", "新班级"))
        self.assertIsNone(self.student.graduation_month)

    def test_cadre_or_account_state_changes_reject_stale_edit(self):
        self.client.force_login(self.admin)
        token = self.edit_token()
        CadreTenure.objects.create(student=self.student, position="新干部职务", starts_on=date(2026, 9, 1))
        self.assertEqual(self.client.post(reverse("account_edit", args=[self.student.pk]), self.profile_data(token)).status_code, 409)
        token = self.edit_token()
        self.client.post(reverse("account_status", args=[self.student.pk, "disable"]))
        self.assertEqual(self.client.post(reverse("account_edit", args=[self.student.pk]), self.profile_data(token)).status_code, 409)
        self.student.refresh_from_db()
        self.assertFalse(self.student.is_active)

    def test_missing_tampered_or_other_admin_edit_tokens_are_rejected(self):
        self.client.force_login(self.admin)
        first_admin_token = self.edit_token()
        other = User.objects.create_user(username="review-other-manager", full_name="另一管理员", is_staff=True)
        self.client.force_login(other)
        for token in ["", "tampered", first_admin_token]:
            self.assertEqual(self.client.post(reverse("account_edit", args=[self.student.pk]), self.profile_data(token)).status_code, 409)
        self.student.refresh_from_db()
        self.assertEqual(self.student.full_name, "学生")

    def test_bulk_update_conflict_rejects_entire_batch_and_keeps_preview(self):
        other = User.objects.create_user(username="review-other-student", full_name="另一学生")
        content = io.StringIO(newline="")
        writer = csv.writer(content)
        writer.writerow(STUDENT_IMPORT_HEADERS)
        for user in [self.student, other]:
            writer.writerow([user.username, "CSV姓名", "110000199001010001", "在校", "无", "CSV班级", "群众", "学院", ""])
        rows = parse_student_accounts(SimpleUploadedFile("students.csv", content.getvalue().encode("utf-8-sig")), mode="update")
        batch = StudentImportBatch.objects.create(uploaded_by=self.admin, mode="update", row_count=2, payload=rows)
        User.objects.filter(pk=other.pk).update(student_class="其他管理员的新班级")
        from django.core.exceptions import ValidationError
        with self.assertRaisesMessage(ValidationError, "本次未更新任何资料"):
            confirm_student_accounts(batch.pk, self.admin)
        self.student.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.student.full_name, "学生")
        self.assertEqual(other.student_class, "其他管理员的新班级")
        batch.refresh_from_db()
        self.assertFalse(batch.is_confirmed)
        self.assertEqual(len(batch.payload), 2)

    def test_delete_review_rejects_students_whose_filterable_profile_changed(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("account_delete_prepare"), {"ids": [self.student.pk]})
        User.objects.filter(pk=self.student.pk).update(student_class="已调整的新班级")
        self.assertEqual(self.client.post(response.url, {"confirmed": "on"}).status_code, 400)
        self.assertTrue(User.objects.filter(pk=self.student.pk).exists())

    def test_repeated_empty_login_posts_return_validation_errors_instead_of_server_error(self):
        for _ in range(6):
            self.assertEqual(self.client.post(reverse("login"), {}).status_code, 200)

    def test_reset_password_never_overwrites_other_account_fields(self):
        original_save = SetPasswordForm.save

        def concurrent_change(form, commit=True):
            User.objects.filter(pk=form.user.pk).update(is_archived=True, is_active=False, session_revision=1, full_name="已核对的新姓名")
            return original_save(form, commit=commit)

        self.client.force_login(self.admin)
        with patch("portal.views.SetPasswordForm.save", concurrent_change):
            self.client.post(reverse("account_reset", args=[self.student.pk]),
                {"new_password1": "Reset!Maple2026", "new_password2": "Reset!Maple2026"})
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_archived)
        self.assertFalse(self.student.is_active)
        self.assertEqual(self.student.full_name, "已核对的新姓名")
        self.assertEqual(self.student.session_revision, 2)
        self.assertTrue(self.student.check_password("Reset!Maple2026"))

    def test_failed_notification_upload_rolls_back_database_and_new_file(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder):
            self.client.force_login(self.admin)
            with patch("portal.views.audit", side_effect=IntegrityError("simulated failure")):
                with self.assertRaises(IntegrityError):
                    self.client.post(reverse("notification_create"), {"title": "失败通知", "category": "other", "body": "正文",
                        "attachment": SimpleUploadedFile("notice.pdf", b"%PDF-1.4 test")})
            self.assertFalse(Notification.objects.exists())
            from pathlib import Path
            self.assertEqual([path for path in Path(folder).rglob("*") if path.is_file()], [])

    def test_failed_video_upload_rolls_back_database_and_new_file(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder):
            self.client.force_login(self.admin)
            with patch("portal.views.audit", side_effect=IntegrityError("simulated failure")):
                with self.assertRaises(IntegrityError):
                    self.client.post(reverse("video_create"), {"title": "失败视频", "category": "other",
                        "file": SimpleUploadedFile("video.mp4", b"\x00\x00\x00\x18ftypisom" + b"0" * 100)})
            self.assertFalse(Video.objects.exists())
            from pathlib import Path
            self.assertEqual([path for path in Path(folder).rglob("*") if path.is_file()], [])

    def test_failed_policy_upload_preserves_old_file_and_version(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder):
            self.client.force_login(self.admin)
            item = Policy.objects.create(created_by=self.admin, status="published")
            revision = PolicyRevision.objects.create(policy=item, title="原制度", body="原正文", category="daily", is_draft=False,
                attachment=SimpleUploadedFile("old.pdf", b"%PDF-1.4 original"))
            item.published_revision = revision
            item.save()
            original_path = revision.attachment.path
            with patch("portal.views.audit", side_effect=IntegrityError("simulated failure")):
                with self.assertRaises(IntegrityError):
                    self.client.post(reverse("policy_edit", args=[item.pk]), {"title": "新草稿", "category": "party", "body": "新正文",
                        "attachment": SimpleUploadedFile("new.pdf", b"%PDF-1.4 new")})
            from pathlib import Path
            self.assertEqual([path for path in Path(folder).rglob("*") if path.is_file()], [Path(original_path)])
            self.assertEqual(PolicyRevision.objects.count(), 1)
            item.refresh_from_db()
            self.assertEqual(item.published_revision_id, revision.pk)

    def test_failed_multi_attachment_guide_upload_leaves_no_orphan_files(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder):
            self.client.force_login(self.admin)
            with patch("portal.views.audit", side_effect=IntegrityError("simulated failure")):
                with self.assertRaises(IntegrityError):
                    self.client.post(reverse("guide_create"), {"title": "失败指南", "category": "daily", "owner": "负责人", "contact": "12345678",
                        "office": "办公室", "process": "办理流程", "attachments": [SimpleUploadedFile("one.pdf", b"%PDF-1.4 one"), SimpleUploadedFile("two.pdf", b"%PDF-1.4 two")]})
            self.assertFalse(ServiceGuide.objects.exists())
            self.assertFalse(GuideAttachment.objects.exists())
            from pathlib import Path
            self.assertEqual([path for path in Path(folder).rglob("*") if path.is_file()], [])

    def test_faq_publish_updates_status_without_overwriting_other_fields(self):
        item = FAQ.objects.create(question="原问题", answer="原回答", category="other", created_by=self.admin)
        original_save = FAQ.save

        def update_answer(instance, *args, **kwargs):
            FAQ.objects.filter(pk=instance.pk).update(answer="已保存的新回答")
            return original_save(instance, *args, **kwargs)

        self.client.force_login(self.admin)
        with patch("portal.views.FAQ.save", update_answer):
            self.client.post(reverse("faq_action", args=[item.pk, "publish"]))
        item.refresh_from_db()
        self.assertEqual(item.answer, "已保存的新回答")
        self.assertEqual(item.status, "published")

    def test_faq_publish_rolls_back_when_audit_fails(self):
        item = FAQ.objects.create(question="原问题", answer="原回答", category="other", created_by=self.admin)
        self.client.force_login(self.admin)
        with patch("portal.views.audit", side_effect=IntegrityError("simulated failure")):
            with self.assertRaises(IntegrityError):
                self.client.post(reverse("faq_action", args=[item.pk, "publish"]))
        item.refresh_from_db()
        self.assertEqual(item.status, "draft")
