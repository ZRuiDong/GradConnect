import tempfile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from .models import Policy, PolicyRevision, User, Video


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ResourceCategoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(username="resource-manager", full_name="管理员", is_staff=True)
        cls.student = User.objects.create_user(username="resource-student", full_name="学生")
        cls.policy = Policy.objects.create(created_by=cls.admin, status="published")
        cls.revision = PolicyRevision.objects.create(policy=cls.policy, title="日常请假制度", summary="申请办理规则",
            body="公开的请假办理流程", category="daily", is_draft=False)
        cls.policy.published_revision = cls.revision
        cls.policy.save()
        cls.private = Policy.objects.create(created_by=cls.admin)
        PolicyRevision.objects.create(policy=cls.private, title="未发布党建制度", body="未公开的内容", category="party")

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        setting = override_settings(MEDIA_ROOT=folder.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.client.force_login(self.student)

    def video(self, category="party", **extra):
        return Video.objects.create(title="党建学习视频", category=category, description="支部学习内容", uploaded_by=self.admin,
            file=SimpleUploadedFile("course.mp4", b"\x00\x00\x00\x18ftypisom" + b"0" * 100), **extra)

    def upload(self):
        return SimpleUploadedFile("course.mp4", b"\x00\x00\x00\x18ftypisom" + b"0" * 100)

    def test_exact_categories_and_labels_for_both_roles(self):
        self.assertEqual(list(PolicyRevision.Category.labels), ["校纪校规", "日常事务", "教务", "评奖评优", "竞赛", "党建", "后勤管理及校园安全", "其他"])
        self.assertEqual(list(Video.Category.labels), ["党建", "班团", "其他"])
        for user in [self.admin, self.student]:
            self.client.force_login(user)
            for name, labels in [("policy_list", PolicyRevision.Category.labels), ("video_list", Video.Category.labels)]:
                response = self.client.get(reverse(name))
                for label in labels:
                    self.assertContains(response, label)

    def test_new_upload_category_starts_blank_but_selected_filter_carries_over(self):
        self.client.force_login(self.admin)
        for name, category in [("policy_create", "safety"), ("video_create", "class_group")]:
            response = self.client.get(reverse(name))
            self.assertEqual(response.context["form"]["category"].value(), "")
            response = self.client.get(reverse(name), {"category": category})
            self.assertEqual(response.context["form"]["category"].value(), category)

    def test_policy_requires_valid_category_before_saving_draft(self):
        self.client.force_login(self.admin)
        before = Policy.objects.count()
        for category in ["", "invalid", "校纪校规"]:
            response = self.client.post(reverse("policy_create"), {"title": "新制度", "body": "正文", "category": category})
            self.assertIn("category", response.context["form"].errors)
        self.assertEqual(Policy.objects.count(), before)
        self.client.post(reverse("policy_create"), {"title": "校园安全规范", "body": "安全要求", "category": "safety"})
        item = Policy.objects.latest("pk")
        self.assertEqual(item.revisions.get().category, "safety")
        self.assertEqual(item.status, "draft")

    def test_video_upload_requires_category_and_saves_selection(self):
        self.client.force_login(self.admin)
        for category in ["", "invalid", "班团"]:
            response = self.client.post(reverse("video_create"), {"title": "班团学习", "category": category, "file": self.upload()})
            self.assertIn("category", response.context["form"].errors)
        self.assertFalse(Video.objects.exists())
        self.client.post(reverse("video_create"), {"title": "班团学习", "category": "class_group", "file": self.upload()})
        self.assertEqual(Video.objects.get().category, "class_group")

    def test_policy_category_keyword_and_summary_combination(self):
        response = self.client.get(reverse("policy_list"), {"category": "daily", "q": "申请办理"})
        self.assertEqual(list(response.context["page_obj"]), [self.policy])
        self.assertContains(response, "日常事务")
        self.assertEqual(self.client.get(reverse("policy_list"), {"category": "party", "q": "申请办理"}).context["page_obj"].paginator.count, 0)

    def test_student_category_only_uses_published_revision_then_changes_on_publish(self):
        draft = PolicyRevision.objects.create(policy=self.policy, number=2, title="新的党建制度", body="保密草稿", category="party")
        response = self.client.get(reverse("policy_list"), {"category": "daily"})
        self.assertEqual(list(response.context["page_obj"]), [self.policy])
        self.assertNotContains(response, draft.title)
        self.assertEqual(self.client.get(reverse("policy_list"), {"category": "party"}).context["page_obj"].paginator.count, 0)
        self.assertEqual(self.client.get(reverse("policy_list"), {"category": "daily", "q": "保密草稿"}).context["page_obj"].paginator.count, 0)
        self.client.force_login(self.admin)
        self.client.post(reverse("policy_action", args=[self.policy.pk, "publish"]))
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("policy_list"), {"category": "daily"}).context["page_obj"].paginator.count, 0)
        self.assertContains(self.client.get(reverse("policy_list"), {"category": "party"}), draft.title)

    def test_admin_filters_displayed_draft_and_never_mixes_fields_from_different_versions(self):
        draft = PolicyRevision.objects.create(policy=self.policy, number=2, title="新的竞赛制度", body="草稿关键词", category="competition")
        self.client.force_login(self.admin)
        response = self.client.get(reverse("policy_list"), {"category": "competition", "q": "草稿关键词"})
        self.assertEqual(list(response.context["page_obj"]), [self.policy])
        self.assertEqual(response.context["page_obj"][0].display_revision.pk, draft.pk)
        for params in [{"category": "daily"}, {"category": "competition", "q": self.revision.title}, {"category": "daily", "q": "草稿关键词"}]:
            self.assertEqual(self.client.get(reverse("policy_list"), params).context["page_obj"].paginator.count, 0)

    def test_old_history_category_does_not_match_current_version(self):
        PolicyRevision.objects.create(policy=self.policy, number=2, title="旧历史版本", category="safety", body="历史正文", is_draft=False)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("policy_list"), {"category": "safety"}).context["page_obj"].paginator.count, 0)
        self.assertContains(self.client.get(reverse("policy_list"), {"category": "daily"}), self.revision.title)

    def test_student_never_reads_draft_or_withdrawn_policy_via_category(self):
        Policy.objects.filter(pk=self.policy.pk).update(status="withdrawn")
        for category in ["daily", "party", ""]:
            self.assertEqual(self.client.get(reverse("policy_list"), {"category": category}).context["page_obj"].paginator.count, 0)
        self.assertEqual(self.client.get(reverse("policy_detail", args=[self.policy.pk])).status_code, 404)

    def test_video_category_keyword_and_deleted_visibility(self):
        wanted = self.video()
        self.video("class_group")
        self.video("party", is_deleted=True)
        response = self.client.get(reverse("video_list"), {"category": "party", "q": "支部学习"})
        self.assertEqual(list(response.context["page_obj"]), [wanted])
        self.assertEqual(len(self.client.get(reverse("video_list"), {"category": "other"}).context["page_obj"]), 0)

    def test_category_labels_are_searchable(self):
        self.assertContains(self.client.get(reverse("policy_list"), {"q": "日常事务"}), self.revision.title)
        item = self.video("class_group")
        self.assertContains(self.client.get(reverse("video_list"), {"q": "班团"}), item.title)

    def test_invalid_filter_returns_no_unfiltered_data(self):
        self.video()
        for name in ["policy_list", "video_list"]:
            for params in [{"category": "bad"}, {"q": "x" * 201}]:
                response = self.client.get(reverse(name), params)
                self.assertTrue(response.context["filter_form"].errors)
                self.assertEqual(response.context["page_obj"].paginator.count, 0)

    def test_pagination_and_category_tabs_keep_keywords(self):
        self.client.force_login(self.admin)
        for index in range(14):
            item = Policy.objects.create(created_by=self.admin)
            PolicyRevision.objects.create(policy=item, title=f"分页制度{index}", body="流程", category="daily")
        response = self.client.get(reverse("policy_list"), {"category": "daily", "q": "分页", "page": 2})
        self.assertEqual(len(response.context["page_obj"]), 2)
        self.assertIn("category=daily", response.context["querystring"])
        for tab in response.context["category_tabs"]:
            self.assertIn("q=", tab["url"])
            self.assertNotIn("page=", tab["url"])

    def test_video_info_edit_changes_category_without_reupload_or_permission_change(self):
        item = self.video()
        original_file, original_uploader = item.file.name, item.uploaded_by_id
        self.client.force_login(self.admin)
        self.client.post(reverse("video_edit", args=[item.pk]), {"title": "改为班团学习", "category": "class_group", "description": "新介绍",
                                                                  "uploaded_by": self.student.pk, "is_deleted": "on"})
        item.refresh_from_db()
        self.assertEqual((item.file.name, item.uploaded_by_id, item.is_deleted), (original_file, original_uploader, False))
        self.assertEqual(item.category, "class_group")
        self.client.force_login(self.student)
        self.assertContains(self.client.get(reverse("video_list"), {"category": "class_group"}), item.title)
        response = self.client.get(reverse("video_file", args=[item.pk]), HTTP_RANGE="bytes=4-7")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(b"".join(response.streaming_content), b"ftyp")

    def test_video_edit_requires_category_and_deleted_video_cannot_be_edited(self):
        item = self.video()
        self.client.force_login(self.admin)
        response = self.client.post(reverse("video_edit", args=[item.pk]), {"title": "未选择分类", "category": ""})
        self.assertIn("category", response.context["form"].errors)
        self.client.post(reverse("video_delete", args=[item.pk]))
        self.assertEqual(self.client.get(reverse("video_edit", args=[item.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("video_file", args=[item.pk])).status_code, 404)

    def test_student_cannot_upload_edit_or_delete_resources(self):
        item = self.video()
        for name, args in [("policy_create", []), ("policy_edit", [self.policy.pk]), ("video_create", []), ("video_edit", [item.pk])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403)
            self.assertEqual(self.client.post(reverse(name, args=args), {"category": "party"}).status_code, 403)

    def test_anonymous_resources_require_login(self):
        item = self.video()
        self.client.logout()
        for name, args in [("policy_list", []), ("video_list", []), ("video_detail", [item.pk]), ("video_edit", [item.pk])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 302)
