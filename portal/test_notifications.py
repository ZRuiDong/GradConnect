import io
import re
import tempfile
from urllib.parse import unquote
from zipfile import ZipFile
from django.contrib.auth.hashers import make_password
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from .forms import NotificationForm
from .models import Audit, Notification, User
from .uploads import NOTIFICATION_FILE_TYPES, NOTIFICATION_MAX_FILE_BYTES


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class NotificationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        password = make_password("Tests!safe2026")
        cls.admin = User.objects.create(username="manager", full_name="管理员", is_staff=True, password=password)
        cls.other_admin = User.objects.create(username="manager-two", full_name="另一管理员", is_staff=True, password=password)
        cls.student = User.objects.create(username="20260001", full_name="学生甲", password=password)
        cls.published = Notification.objects.create(title="新生报到通知", category="admission", summary="入学材料核对",
            body="请关注报到安排及材料提交说明。", created_by=cls.admin, updated_by=cls.admin)
        cls.withdrawn = Notification.objects.create(title="撤下的招聘通知", category="recruitment", body="尚未公开的通知正文",
            status="withdrawn", created_by=cls.admin)

    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.override = override_settings(MEDIA_ROOT=self.media.name)
        self.override.enable()
        self.addCleanup(self.override.disable)

    def data(self, **extra):
        return {"title": "新的通知", "category": "party", "summary": "通知摘要", "body": "新的通知正文", **extra}

    def pdf(self, content=b"%PDF-1.4 test"):
        return SimpleUploadedFile("notice.pdf", content, content_type="application/pdf")

    def reasons(self, response):
        return {item.title for item in response.context["page_obj"]}

    def test_categories_and_nav_immediately_below_dashboard(self):
        labels = ["新生入学", "党建", "招聘信息", "评奖评优", "竞赛", "文体活动", "离校手续", "其他"]
        self.assertEqual(list(Notification.Category.labels), labels)
        for user in [self.admin, self.student]:
            self.client.force_login(user)
            response = self.client.get(reverse("dashboard"))
            nav = response.content.decode().split('<nav aria-label="主导航">', 1)[1].split("</nav>", 1)[0]
            self.assertEqual(re.findall(r'href="([^"]+)"', nav)[:4], ["/", "/notifications/", "/guides/", "/leaves/"])
            response = self.client.get(reverse("notification_list"))
            for label in labels:
                self.assertContains(response, label)

    def test_anonymous_cannot_read_notifications(self):
        for name in ["notification_list", "notification_create"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 302)
        for name in ["notification_detail", "notification_file", "notification_edit"]:
            self.assertEqual(self.client.get(reverse(name, args=[self.published.pk])).status_code, 302)

    def test_student_cannot_publish_edit_or_withdraw(self):
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("notification_create")).status_code, 403)
        self.assertEqual(self.client.post(reverse("notification_create"), self.data()).status_code, 403)
        url = reverse("notification_edit", args=[self.published.pk])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, self.data()).status_code, 403)
        self.assertEqual(self.client.post(reverse("notification_action", args=[self.published.pk, "withdraw"])).status_code, 403)
        self.assertEqual(Notification.objects.count(), 2)
        self.published.refresh_from_db()
        self.assertEqual(self.published.status, "published")

    def test_admin_can_publish_in_every_category(self):
        self.client.force_login(self.admin)
        for value, label in Notification.Category.choices:
            with self.subTest(category=value):
                form_page = self.client.get(reverse("notification_create"), {"category": value})
                self.assertEqual(form_page.context["form"].initial["category"], value)
                response = self.client.post(reverse("notification_create"), self.data(title=f"{label}专属通知", category=value))
                item = Notification.objects.get(title=f"{label}专属通知")
                self.assertRedirects(response, reverse("notification_detail", args=[item.pk]))
                self.assertEqual(item.category, value)
                self.assertEqual(item.status, "published")
                self.assertEqual(item.created_by, self.admin)

    def test_invalid_category_and_empty_content_rejected(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("notification_create"), self.data(category="invalid"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors.get("category"))
        self.assertContains(self.client.post(reverse("notification_create"), self.data(body="")), "请填写通知正文或上传附件")
        self.assertEqual(Notification.objects.count(), 2)

    def test_posted_permissions_and_creator_are_ignored(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("notification_create"), self.data(status="withdrawn", created_by=self.student.pk, updated_by=self.student.pk))
        item = Notification.objects.get(title="新的通知")
        self.assertEqual(item.status, "published")
        self.assertEqual(item.created_by, self.admin)
        self.assertEqual(item.updated_by, self.admin)

    def test_student_searches_title_summary_and_body(self):
        self.client.force_login(self.student)
        for q in ["新生报到", "入学材料", "材料提交"]:
            self.assertEqual(self.reasons(self.client.get(reverse("notification_list"), {"q": q})), {self.published.title})
        self.assertEqual(self.reasons(self.client.get(reverse("notification_list"), {"category": "admission", "q": "报到"})), {self.published.title})
        self.assertEqual(self.reasons(self.client.get(reverse("notification_list"), {"category": "party", "q": "报到"})), set())

    def test_students_never_see_withdrawn_notifications_or_status_controls(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("notification_list"))
        self.assertNotContains(response, self.withdrawn.title)
        self.assertNotContains(response, "发布通知</a>")
        self.assertNotContains(response, 'name="status"')
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        response = self.client.get(reverse("notification_list"), {"q": self.withdrawn.title, "status": "withdrawn"})
        self.assertEqual(response.context["page_obj"].paginator.count, 0)
        self.assertEqual(self.client.get(reverse("notification_detail", args=[self.withdrawn.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("notification_file", args=[self.withdrawn.pk])).status_code, 404)

    def test_admin_status_and_category_filters_combine(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("notification_list"), {"category": "recruitment", "status": "withdrawn", "q": "招聘"})
        self.assertEqual(self.reasons(response), {self.withdrawn.title})
        self.assertContains(response, 'name="status"')
        response = self.client.get(reverse("notification_list"), {"category": "admission", "status": "withdrawn"})
        self.assertEqual(self.reasons(response), set())

    def test_invalid_search_does_not_show_unfiltered_results(self):
        self.client.force_login(self.admin)
        for params in [{"category": "invalid"}, {"status": "invalid"}, {"q": "x" * 201}]:
            response = self.client.get(reverse("notification_list"), params)
            self.assertContains(response, "筛选条件有误")
            self.assertEqual(response.context["page_obj"].paginator.count, 0)

    def test_edit_replaces_content_and_category_preserving_creator(self):
        self.client.force_login(self.other_admin)
        published_at = self.published.published_at
        response = self.client.post(reverse("notification_edit", args=[self.published.pk]), self.data(title="竞赛报名通知", category="competition"))
        self.assertRedirects(response, reverse("notification_detail", args=[self.published.pk]))
        self.published.refresh_from_db()
        self.assertEqual(self.published.created_by, self.admin)
        self.assertEqual(self.published.updated_by, self.other_admin)
        self.assertEqual(self.published.published_at, published_at)
        self.client.force_login(self.student)
        self.assertEqual(self.reasons(self.client.get(reverse("notification_list"), {"q": "新生报到"})), set())
        self.assertEqual(self.reasons(self.client.get(reverse("notification_list"), {"category": "competition"})), {"竞赛报名通知"})

    def test_withdraw_edit_and_republish(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("notification_action", args=[self.published.pk, "withdraw"]))
        self.assertContains(self.client.get(reverse("notification_detail", args=[self.published.pk])), "该通知已撤下")
        self.client.post(reverse("notification_edit", args=[self.published.pk]), self.data(status="published"))
        self.published.refresh_from_db()
        self.assertEqual(self.published.status, "withdrawn")
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("notification_detail", args=[self.published.pk])).status_code, 404)
        self.client.force_login(self.admin)
        self.client.post(reverse("notification_action", args=[self.published.pk, "publish"]))
        self.client.force_login(self.student)
        self.assertContains(self.client.get(reverse("notification_detail", args=[self.published.pk])), "新的通知正文")

    def test_state_actions_are_post_only_and_idempotent(self):
        self.client.force_login(self.admin)
        url = reverse("notification_action", args=[self.published.pk, "withdraw"])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.post(url)
        self.client.post(url)
        self.assertEqual(Audit.objects.filter(action="撤下通知").count(), 1)
        self.assertEqual(self.client.post(reverse("notification_action", args=[self.published.pk, "delete"])).status_code, 404)
        self.client.post(reverse("notification_action", args=[self.published.pk, "publish"]))
        self.published.refresh_from_db()
        stamp = self.published.published_at
        self.client.post(reverse("notification_action", args=[self.published.pk, "publish"]))
        self.published.refresh_from_db()
        self.assertEqual(self.published.published_at, stamp)

    def test_notifications_mutations_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post(reverse("notification_create"), self.data()).status_code, 403)
        self.assertEqual(client.post(reverse("notification_edit", args=[self.published.pk]), self.data()).status_code, 403)
        self.assertEqual(client.post(reverse("notification_action", args=[self.published.pk, "withdraw"])).status_code, 403)

    def test_pdf_only_notice_preview_download_and_withdrawal(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("notification_create"), self.data(body="", attachment=self.pdf()))
        item = Notification.objects.get(title="新的通知")
        self.client.force_login(self.student)
        self.assertContains(self.client.get(reverse("notification_detail", args=[item.pk])), "PDF 预览")
        url = reverse("notification_file", args=[item.pk])
        response = self.client.get(url)
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4 test")
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        response.close()
        response = self.client.get(url + "?download=1")
        self.assertIn("attachment", response["Content-Disposition"])
        response.close()
        self.client.force_login(self.admin)
        self.client.post(reverse("notification_action", args=[item.pk, "withdraw"]))
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(url + "?download=1").status_code, 404)

    def test_attachment_signature_validated_and_missing_attachment_is_404(self):
        form = NotificationForm(data=self.data(), files={"attachment": self.pdf(b"not a PDF")})
        self.assertFalse(form.is_valid())
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("notification_file", args=[self.published.pk])).status_code, 404)

    def test_attachment_can_be_replaced_or_removed(self):
        self.published.attachment.save("old.pdf", self.pdf(b"%PDF-old"))
        old_path = self.published.attachment.name
        self.client.force_login(self.admin)
        self.client.post(reverse("notification_edit", args=[self.published.pk]), self.data(attachment=self.pdf(b"%PDF-new")))
        self.published.refresh_from_db()
        self.assertNotEqual(self.published.attachment.name, old_path)
        self.client.force_login(self.student)
        url = reverse("notification_file", args=[self.published.pk])
        response = self.client.get(url)
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-new")
        response.close()
        self.client.force_login(self.admin)
        self.client.post(reverse("notification_edit", args=[self.published.pk]), self.data(**{"attachment-clear": "on"}))
        self.published.refresh_from_db()
        self.assertFalse(self.published.attachment)
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(url).status_code, 404)

    def document(self, extension):
        if extension == ".pdf":
            content = b"%PDF-1.4 notification test"
        elif extension in {".doc", ".xls", ".ppt"}:
            content = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(504)
        elif extension in {".docx", ".xlsx", ".pptx", ".zip"}:
            output = io.BytesIO()
            with ZipFile(output, "w") as package:
                if extension == ".zip":
                    package.writestr("通知材料.txt", "通知内容")
                else:
                    package.writestr("[Content_Types].xml", "<Types/>")
                    main = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}[extension]
                    package.writestr(main, "<document/>")
            content = output.getvalue()
        else:
            content = {".jpg": b"\xff\xd8\xff test", ".jpeg": b"\xff\xd8\xff test", ".png": b"\x89PNG\r\n\x1a\n test",
                ".gif": b"GIF89a test", ".webp": b"RIFF\x10\x00\x00\x00WEBP test", ".bmp": b"BM test",
                ".txt": "通知正文\n文字附件".encode(), ".csv": "学号,姓名\n0001,张同学\n".encode()}[extension]
        return SimpleUploadedFile("通知附件" + extension, content, content_type="application/octet-stream")

    def test_all_common_formats_can_publish_without_body_and_download_original_bytes(self):
        for extension, content_type in NOTIFICATION_FILE_TYPES.items():
            with self.subTest(extension=extension):
                self.client.force_login(self.admin)
                upload = self.document(extension)
                original = upload.read()
                upload.seek(0)
                response = self.client.post(reverse("notification_create"), self.data(title="附件通知" + extension, body="", attachment=upload))
                item = Notification.objects.get(title="附件通知" + extension)
                self.assertRedirects(response, reverse("notification_detail", args=[item.pk]))
                self.client.force_login(self.student)
                detail = self.client.get(reverse("notification_detail", args=[item.pk]))
                is_image = content_type.startswith("image/")
                self.assertContains(detail, "下载附件")
                self.assertEqual(b'class="pdf-preview"' in detail.content, extension == ".pdf")
                self.assertEqual(b'class="notification-image-preview"' in detail.content, is_image)
                for query in ["", "?download=1"]:
                    response = self.client.get(reverse("notification_file", args=[item.pk]) + query)
                    self.assertEqual(response["Content-Type"], content_type)
                    self.assertEqual(b"".join(response.streaming_content), original)
                    expected = "attachment" if query or not (is_image or extension == ".pdf") else "inline"
                    self.assertTrue(response["Content-Disposition"].startswith(expected + ";"))
                    self.assertTrue(unquote(response["Content-Disposition"]).endswith(extension))
                    self.assertEqual(response["X-Content-Type-Options"], "nosniff")
                    self.assertEqual(response["Cache-Control"], "private, no-store")
                    response.close()

    def test_attachment_rejects_unsupported_disguised_empty_and_oversized_files(self):
        invalid = [SimpleUploadedFile("notice" + extension, b"<script>test</script>")
                   for extension in [".html", ".svg", ".exe", ".docx", ".pdf", ".png", ".jpg", ".gif", ".webp", ".bmp", ".ppt", ".pptx", ".zip"]]
        invalid.extend([SimpleUploadedFile("empty.txt", b""), SimpleUploadedFile("binary.csv", b"\x00\x01\x02"),
                        SimpleUploadedFile("encoding.txt", b"\xff\xff\xff"), self.document(".xlsx")])
        invalid[-1].name = "wrong.pptx"
        oversized = self.document(".pdf")
        oversized.size = NOTIFICATION_MAX_FILE_BYTES + 1
        invalid.append(oversized)
        for upload in invalid:
            with self.subTest(filename=upload.name):
                form = NotificationForm(data=self.data(), files={"attachment": upload})
                self.assertFalse(form.is_valid())
                self.assertIn("attachment", form.errors)
        self.assertEqual(Notification.objects.count(), 2)

    def test_text_encodings_and_uppercase_extension_are_accepted(self):
        for encoding in ["utf-8-sig", "gb18030", "utf-16"]:
            with self.subTest(encoding=encoding):
                form = NotificationForm(data=self.data(), files={"attachment": SimpleUploadedFile("通知.TXT", "中文通知\r\n正文".encode(encoding))})
                self.assertTrue(form.is_valid(), form.errors)
        upload = self.document(".docx")
        upload.name = "通知.DOCX"
        self.client.force_login(self.admin)
        self.client.post(reverse("notification_create"), self.data(attachment=upload))
        item = Notification.objects.get(title="新的通知")
        response = self.client.get(reverse("notification_file", args=[item.pk]))
        self.assertEqual(response["Content-Type"], NOTIFICATION_FILE_TYPES[".docx"])
        response.close()

    def test_non_pdf_files_withdraw_and_republish_keep_access_rules(self):
        for extension in [".docx", ".png"]:
            with self.subTest(extension=extension):
                self.client.force_login(self.admin)
                self.client.post(reverse("notification_create"), self.data(title="撤下附件" + extension, attachment=self.document(extension)))
                item = Notification.objects.get(title="撤下附件" + extension)
                self.client.post(reverse("notification_action", args=[item.pk, "withdraw"]))
                self.client.force_login(self.student)
                url = reverse("notification_file", args=[item.pk])
                for query in ["", "?download=1"]:
                    self.assertEqual(self.client.get(url + query).status_code, 404)
                self.client.force_login(self.admin)
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                response.close()
                self.client.post(reverse("notification_action", args=[item.pk, "publish"]))
                self.client.force_login(self.student)
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_replacing_pdf_with_word_then_image_updates_view_and_retains_on_text_edit(self):
        self.published.attachment.save("original.pdf", self.pdf())
        self.client.force_login(self.admin)
        url = reverse("notification_edit", args=[self.published.pk])
        for extension in [".docx", ".png"]:
            self.client.post(url, self.data(attachment=self.document(extension)))
            self.published.refresh_from_db()
            name = self.published.attachment.name
            self.client.post(url, self.data(summary="只更新摘要"))
            self.published.refresh_from_db()
            self.assertEqual(self.published.attachment.name, name)
            detail = self.client.get(reverse("notification_detail", args=[self.published.pk]))
            self.assertNotContains(detail, 'class="pdf-preview"')
            self.assertEqual(b'class="notification-image-preview"' in detail.content, extension == ".png")
        response = self.client.post(url, self.data(body="", **{"attachment-clear": "on"}))
        self.assertContains(response, "请填写通知正文或上传附件")
        self.published.refresh_from_db()
        self.assertTrue(self.published.attachment)
        self.client.post(url, self.data(**{"attachment-clear": "on"}))
        self.assertEqual(self.client.get(reverse("notification_file", args=[self.published.pk])).status_code, 404)

    @override_settings(USE_X_ACCEL_REDIRECT=True)
    def test_non_pdf_production_internal_delivery_uses_correct_mime_and_download(self):
        self.published.attachment.save("notice.docx", self.document(".docx"))
        self.client.force_login(self.student)
        response = self.client.get(reverse("notification_file", args=[self.published.pk]))
        self.assertEqual(response["Content-Type"], NOTIFICATION_FILE_TYPES[".docx"])
        self.assertTrue(response["Content-Disposition"].startswith("attachment;"))
        self.assertIn("/_protected/notification/", response["X-Accel-Redirect"])
        self.assertTrue(response["X-Accel-Redirect"].endswith(".docx"))

    def test_category_filter_and_search_persist_across_pages(self):
        for index in range(14):
            Notification.objects.create(title=f"竞赛专属通知{index}", category="competition", body="公开说明", created_by=self.admin)
        self.client.force_login(self.student)
        response = self.client.get(reverse("notification_list"), {"category": "competition", "q": "专属", "page": "2"})
        self.assertEqual(response.context["page_obj"].paginator.count, 14)
        self.assertEqual(len(response.context["page_obj"]), 2)
        self.assertIn("category=competition", response.context["querystring"])
        for tab in response.context["category_tabs"]:
            self.assertNotIn("page=2", tab["url"])
            self.assertIn("q=", tab["url"])

    def test_notice_text_is_html_escaped(self):
        self.published.body = '<script>alert("x")</script>'
        self.published.save()
        self.client.force_login(self.student)
        response = self.client.get(reverse("notification_detail", args=[self.published.pk]))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, '<script>alert("x")</script>')

    def test_publish_update_and_withdraw_are_audited(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("notification_create"), self.data())
        item = Notification.objects.get(title="新的通知")
        self.client.post(reverse("notification_edit", args=[item.pk]), self.data(title="更新后的通知"))
        self.client.post(reverse("notification_action", args=[item.pk, "withdraw"]))
        self.assertEqual(set(Audit.objects.filter(target=f"portal.Notification:{item.pk}").values_list("action", flat=True)),
                         {"发布通知", "更新通知", "撤下通知"})
