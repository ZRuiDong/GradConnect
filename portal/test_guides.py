import io
import re
import tempfile
from zipfile import ZipFile
from django.contrib.auth.hashers import make_password
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from .forms import ServiceGuideForm
from .models import Audit, Conversation, GuideAttachment, Matter, Message, ServiceGuide, User
from .uploads import GUIDE_FILE_TYPES, GUIDE_MAX_FILE_BYTES


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ServiceGuideTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        password = make_password("Tests!safe2026")
        cls.admin = User.objects.create(username="manager", full_name="管理员", is_staff=True, password=password)
        cls.other_admin = User.objects.create(username="manager-two", full_name="另一管理员", is_staff=True, password=password)
        cls.student = User.objects.create(username="20260001", full_name="学生甲", password=password)
        cls.other = User.objects.create(username="20260002", full_name="学生乙", password=password)
        cls.guide = ServiceGuide.objects.create(title="学生证补办流程", category="daily", owner="王老师", contact="12345678",
            office="服务中心101", process="1.下载申请表。\n2.提交材料办理补办。", created_by=cls.admin, updated_by=cls.admin)
        cls.withdrawn = ServiceGuide.objects.create(title="已撤下的教务流程", category="academic", owner="李老师", contact="87654321",
            office="教务办公室", process="尚未公开的办理步骤", status="withdrawn", created_by=cls.admin)

    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.override = override_settings(MEDIA_ROOT=self.media.name)
        self.override.enable()
        self.addCleanup(self.override.disable)

    def data(self, **extra):
        return {"title": "新的办事指南", "category": "awards", "owner": "张老师", "contact": "12340000 / student@example.edu",
                "office": "学院办公室201", "process": "1.准备材料。\n2.提交申请表。\n3.到办公室办理。", **extra}

    def document(self, extension=".pdf", name=None):
        if extension == ".pdf":
            content = b"%PDF-1.4 guide test"
        elif extension in {".doc", ".xls"}:
            content = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(504)
        else:
            output = io.BytesIO()
            with ZipFile(output, "w") as package:
                package.writestr("[Content_Types].xml", "<Types/>")
                package.writestr("word/document.xml" if extension == ".docx" else "xl/workbook.xml", "<document/>")
            content = output.getvalue()
        return SimpleUploadedFile(name or "办理表单" + extension, content, content_type=GUIDE_FILE_TYPES[extension])

    def attachment(self, guide=None, extension=".pdf"):
        upload = self.document(extension)
        return GuideAttachment.objects.create(guide=guide or self.guide, file=upload, original_name=upload.name)

    def titles(self, response):
        return {item.title for item in response.context["page_obj"]}

    def test_eight_categories_and_navigation_order_for_both_roles(self):
        self.assertEqual(list(ServiceGuide.Category.labels), ["日常事务", "评奖评优", "新生入学", "离校手续", "党建", "后勤", "教务", "其他"])
        for user in [self.admin, self.student]:
            self.client.force_login(user)
            response = self.client.get(reverse("dashboard"))
            nav = response.content.decode().split('<nav aria-label="主导航">', 1)[1].split("</nav>", 1)[0]
            self.assertEqual(re.findall(r'href="([^"]+)"', nav)[:4], ["/", "/notifications/", "/guides/", "/leaves/"])
            self.assertNotContains(response, "在办事项")
            self.assertContains(response, self.guide.title)
            for label in ServiceGuide.Category.labels:
                self.assertContains(self.client.get(reverse("guide_list")), label)

    def test_anonymous_and_student_management_permissions(self):
        attachment = self.attachment()
        for url in [reverse("guide_list"), reverse("guide_detail", args=[self.guide.pk]),
                    reverse("guide_file", args=[self.guide.pk, attachment.pk])]:
            self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.student)
        for url in [reverse("guide_create"), reverse("guide_edit", args=[self.guide.pk])]:
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, self.data()).status_code, 403)
        self.assertEqual(self.client.post(reverse("guide_action", args=[self.guide.pk, "withdraw"])).status_code, 403)
        self.assertEqual(ServiceGuide.objects.count(), 2)

    def test_admin_can_publish_every_category_with_required_fields(self):
        self.client.force_login(self.admin)
        for value, label in ServiceGuide.Category.choices:
            response = self.client.get(reverse("guide_create"), {"category": value})
            self.assertEqual(response.context["form"].initial["category"], value)
            response = self.client.post(reverse("guide_create"), self.data(title=label + "测试指南", category=value))
            item = ServiceGuide.objects.get(title=label + "测试指南")
            self.assertRedirects(response, reverse("guide_detail", args=[item.pk]))
            self.assertEqual(item.status, "published")
            self.assertEqual(item.created_by, self.admin)

    def test_required_metadata_and_process_are_validated(self):
        for field in ["title", "category", "owner", "contact", "office", "process"]:
            with self.subTest(field=field):
                form = ServiceGuideForm(data=self.data(**{field: ""}))
                self.assertFalse(form.is_valid())
                self.assertIn(field, form.errors)
        self.assertFalse(ServiceGuideForm(data=self.data(category="unknown")).is_valid())

    def test_search_category_and_pagination_for_student(self):
        self.client.force_login(self.student)
        for q in ["补办", "王老师", "12345678", "服务中心101", "申请表"]:
            self.assertEqual(self.titles(self.client.get(reverse("guide_list"), {"q": q})), {self.guide.title})
        self.assertEqual(self.titles(self.client.get(reverse("guide_list"), {"category": "awards", "q": "补办"})), set())
        for index in range(14):
            ServiceGuide.objects.create(**self.data(title=f"后勤办理{index}", category="logistics"), created_by=self.admin)
        response = self.client.get(reverse("guide_list"), {"category": "logistics", "q": "后勤", "page": 2})
        self.assertEqual(response.context["page_obj"].paginator.count, 14)
        self.assertEqual(len(response.context["page_obj"]), 2)
        self.assertIn("category=logistics", response.context["querystring"])
        for tab in response.context["category_tabs"]:
            self.assertNotIn("page=2", tab["url"])

    def test_withdrawn_guide_hidden_from_students_and_visible_to_admin(self):
        attachment = self.attachment(self.withdrawn)
        self.client.force_login(self.student)
        self.assertNotContains(self.client.get(reverse("guide_list")), self.withdrawn.title)
        response = self.client.get(reverse("guide_list"), {"category": "academic", "status": "withdrawn", "q": "办理步骤"})
        self.assertEqual(response.context["page_obj"].paginator.count, 0)
        self.assertEqual(self.client.get(reverse("guide_detail", args=[self.withdrawn.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("guide_file", args=[self.withdrawn.pk, attachment.pk])).status_code, 404)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("guide_list"), {"category": "academic", "status": "withdrawn"})
        self.assertEqual(self.titles(response), {self.withdrawn.title})

    def test_invalid_filters_do_not_show_everything(self):
        self.client.force_login(self.admin)
        for params in [{"category": "unknown"}, {"status": "unknown"}, {"q": "x" * 201}]:
            response = self.client.get(reverse("guide_list"), params)
            self.assertContains(response, "筛选条件有误")
            self.assertEqual(response.context["page_obj"].paginator.count, 0)

    def test_replacement_preserves_creator_and_changes_search_results(self):
        self.client.force_login(self.other_admin)
        response = self.client.post(reverse("guide_edit", args=[self.guide.pk]), self.data(title="新生报到流程", category="admission"))
        self.assertRedirects(response, reverse("guide_detail", args=[self.guide.pk]))
        self.guide.refresh_from_db()
        self.assertEqual(self.guide.created_by, self.admin)
        self.assertEqual(self.guide.updated_by, self.other_admin)
        self.client.force_login(self.student)
        self.assertEqual(self.titles(self.client.get(reverse("guide_list"), {"q": "补办"})), set())
        self.assertEqual(self.titles(self.client.get(reverse("guide_list"), {"category": "admission"})), {"新生报到流程"})

    def test_withdraw_edit_and_republish_controls_all_attachments(self):
        attachments = [self.attachment(extension=extension) for extension in GUIDE_FILE_TYPES]
        self.client.force_login(self.admin)
        self.client.post(reverse("guide_action", args=[self.guide.pk, "withdraw"]))
        self.client.post(reverse("guide_edit", args=[self.guide.pk]), self.data(status="published"))
        self.guide.refresh_from_db()
        self.assertEqual(self.guide.status, "withdrawn")
        self.client.force_login(self.student)
        for attachment in attachments:
            self.assertEqual(self.client.get(reverse("guide_file", args=[self.guide.pk, attachment.pk])).status_code, 404)
        self.client.force_login(self.admin)
        self.client.post(reverse("guide_action", args=[self.guide.pk, "publish"]))
        self.client.force_login(self.student)
        self.assertContains(self.client.get(reverse("guide_detail", args=[self.guide.pk])), "准备材料")
        response = self.client.get(reverse("guide_file", args=[self.guide.pk, attachments[0].pk]))
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_multiple_excel_word_and_pdf_uploads_download_with_original_names(self):
        self.client.force_login(self.admin)
        uploads = [self.document(extension) for extension in GUIDE_FILE_TYPES]
        expected = {}
        for upload in uploads:
            expected[upload.name] = upload.read()
            upload.seek(0)
        response = self.client.post(reverse("guide_create"), self.data(attachments=uploads))
        item = ServiceGuide.objects.get(title="新的办事指南")
        self.assertRedirects(response, reverse("guide_detail", args=[item.pk]))
        self.assertEqual(item.attachments.count(), 5)
        self.client.force_login(self.student)
        detail = self.client.get(reverse("guide_detail", args=[item.pk]))
        for attachment in item.attachments.all():
            self.assertContains(detail, attachment.original_name)
            response = self.client.get(reverse("guide_file", args=[item.pk, attachment.pk]))
            self.assertEqual(response["Content-Type"], GUIDE_FILE_TYPES[attachment.extension])
            self.assertEqual(response["Cache-Control"], "private, no-store")
            if not attachment.is_pdf:
                self.assertIn("attachment", response["Content-Disposition"])
            self.assertEqual(b"".join(response.streaming_content), expected[attachment.original_name])
            response.close()

    def test_invalid_or_disguised_documents_are_rejected_without_partial_publish(self):
        self.client.force_login(self.admin)
        bad_files = [SimpleUploadedFile("bad.exe", b"executable"), SimpleUploadedFile("bad.pdf", b"not PDF"),
                     SimpleUploadedFile("bad.doc", b"not Office"), SimpleUploadedFile("bad.xlsx", b"not zip"),
                     self.document(".xlsx", "disguised.docx")]
        for upload in bad_files:
            response = self.client.post(reverse("guide_create"), self.data(attachments=[self.document(), upload]))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["form"].errors.get("attachments"))
            self.assertFalse(ServiceGuide.objects.filter(title="新的办事指南").exists())
        oversized = self.document()
        oversized.size = GUIDE_MAX_FILE_BYTES + 1
        form = ServiceGuideForm(data=self.data(), files={"attachments": [oversized]})
        self.assertFalse(form.is_valid())
        self.assertIn("20 MB", str(form.errors))

    def test_attachment_removal_is_scoped_and_replacement_is_supported(self):
        original = self.attachment(extension=".docx")
        foreign = self.attachment(self.withdrawn, ".xlsx")
        self.client.force_login(self.admin)
        url = reverse("guide_edit", args=[self.guide.pk])
        response = self.client.post(url, self.data(remove_attachments=[foreign.pk]))
        self.assertTrue(response.context["form"].errors.get("remove_attachments"))
        self.assertTrue(GuideAttachment.objects.filter(pk=foreign.pk).exists())
        self.client.post(url, self.data(remove_attachments=[original.pk], attachments=[self.document(".xlsx")]))
        self.assertFalse(GuideAttachment.objects.filter(pk=original.pk).exists())
        self.assertEqual(self.guide.attachments.count(), 1)
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("guide_file", args=[self.guide.pk, original.pk])).status_code, 404)

    def test_attachment_limit_applies_to_existing_plus_new_files(self):
        for _ in range(10):
            self.attachment()
        self.client.force_login(self.admin)
        url = reverse("guide_edit", args=[self.guide.pk])
        self.assertContains(self.client.post(url, self.data(attachments=[self.document()])), "最多保留 10 个附件")
        remove = self.guide.attachments.first()
        self.assertEqual(self.client.post(url, self.data(remove_attachments=[remove.pk], attachments=[self.document()])).status_code, 302)
        self.assertEqual(self.guide.attachments.count(), 10)

    def test_attachment_cannot_be_requested_under_another_guide(self):
        attachment = self.attachment()
        other_guide = ServiceGuide.objects.create(**self.data(), created_by=self.admin)
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("guide_file", args=[other_guide.pk, attachment.pk])).status_code, 404)

    def test_pdf_preview_and_office_download_protection(self):
        pdf = self.attachment()
        doc = self.attachment(extension=".docx")
        self.client.force_login(self.student)
        response = self.client.get(reverse("guide_file", args=[self.guide.pk, pdf.pk]))
        self.assertTrue(response["Content-Disposition"].startswith("inline"))
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
        response.close()
        response = self.client.get(reverse("guide_file", args=[self.guide.pk, pdf.pk]) + "?download=1")
        self.assertIn("attachment", response["Content-Disposition"])
        response.close()
        response = self.client.get(reverse("guide_file", args=[self.guide.pk, doc.pk]))
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        response.close()

    def test_state_actions_require_post_and_csrf(self):
        self.client.force_login(self.admin)
        url = reverse("guide_action", args=[self.guide.pk, "withdraw"])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(reverse("guide_action", args=[self.guide.pk, "delete"])).status_code, 404)
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.admin)
        self.assertEqual(protected.post(url).status_code, 403)
        self.assertEqual(protected.post(reverse("guide_create"), self.data()).status_code, 403)

    def test_markup_is_escaped_and_publication_metadata_cannot_be_forged(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("guide_create"), self.data(process="<script>alert(1)</script>", created_by=self.student.pk, status="withdrawn"))
        item = ServiceGuide.objects.get(title="新的办事指南")
        self.assertEqual(item.created_by, self.admin)
        self.assertEqual(item.status, "published")
        self.client.force_login(self.student)
        response = self.client.get(reverse("guide_detail", args=[item.pk]))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")

    def test_history_consultation_survives_retired_matter_module(self):
        matter = Matter.objects.create(title="历史事项", description="历史说明", created_by=self.admin)
        conversation = Conversation.objects.create(student=self.student, matter=matter, subject="旧事项咨询")
        Message.objects.create(conversation=conversation, author=self.student, body="历史私人问题")
        self.client.force_login(self.student)
        self.assertContains(self.client.get(reverse("consultation_detail", args=[conversation.pk])), "历史私人问题")
        self.assertEqual(self.client.get(reverse("consultation_create") + f"?matter={matter.pk}").status_code, 404)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("consultation_detail", args=[conversation.pk])).status_code, 404)
        self.client.force_login(self.admin)
        self.client.post(reverse("consultation_detail", args=[conversation.pk]), {"body": "历史咨询继续回复"})
        self.assertTrue(Message.objects.filter(conversation=conversation, body="历史咨询继续回复").exists())

    def test_publication_changes_are_audited(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("guide_create"), self.data())
        item = ServiceGuide.objects.get(title="新的办事指南")
        self.client.post(reverse("guide_edit", args=[item.pk]), self.data(title="更新后的指南"))
        self.client.post(reverse("guide_action", args=[item.pk, "withdraw"]))
        self.client.post(reverse("guide_action", args=[item.pk, "publish"]))
        self.assertEqual(set(Audit.objects.filter(target=f"portal.ServiceGuide:{item.pk}").values_list("action", flat=True)),
                         {"发布办事指南", "更新办事指南", "撤下办事指南", "重新发布办事指南"})
