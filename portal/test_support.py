from django.test import TestCase, override_settings
from django.urls import reverse
from types import SimpleNamespace
from urllib.parse import unquote
from xml.etree import ElementTree as ET
from .forms import ConversationForm, FAQForm
from .models import Conversation, FAQ, InquiryCategory, Message, User
from .watermarks import support_watermark


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SupportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(username="support-manager", full_name="管理员", is_staff=True)
        cls.student = User.objects.create_user(username="20260011", full_name="学生甲")
        cls.other = User.objects.create_user(username="20260012", full_name="学生乙")
        cls.faq = FAQ.objects.create(question="补办校园卡", answer="到服务中心办理", category="campus", status="published", created_by=cls.admin)
        cls.draft = FAQ.objects.create(question="草稿问题", answer="未公开答复", category="campus", created_by=cls.admin)
        cls.thread = Conversation.objects.create(student=cls.student, subject="私人实习咨询", category="academic")
        Message.objects.create(conversation=cls.thread, author=cls.student, body="私人信息不能公开")

    def setUp(self):
        self.client.force_login(self.student)

    def test_exact_eight_shared_categories(self):
        self.assertEqual(list(InquiryCategory.labels), ["日常事务", "评奖评优", "新生入学", "离校手续", "党建", "后勤及校园管理", "教务", "其他"])
        self.assertEqual(list(ConversationForm.base_fields["category"].choices)[1:], list(InquiryCategory.choices))
        self.assertEqual(list(FAQForm.base_fields["category"].choices), list(InquiryCategory.choices))
        for name in ["faq_list", "consultation_list"]:
            response = self.client.get(reverse(name))
            for label in InquiryCategory.labels:
                self.assertContains(response, label)

    def test_support_entry_and_submodule_navigation(self):
        self.assertRedirects(self.client.get(reverse("support_home")), reverse("faq_list"))
        for name, subsection in [("faq_list", "faq"), ("consultation_list", "consultation")]:
            response = self.client.get(reverse(name))
            self.assertEqual(response.context["active_nav"], "support")
            self.assertEqual(response.context["active_subnav"], subsection)
            self.assertContains(response, 'aria-label="咨询回复子模块"')
            self.assertNotContains(response, "我的咨询")

    def test_public_faqs_visible_to_every_student_but_drafts_and_withdrawn_hidden(self):
        withdrawn = FAQ.objects.create(question="撤下问题", answer="撤下回答", category="campus", status="withdrawn", created_by=self.admin)
        for user in [self.student, self.other]:
            self.client.force_login(user)
            response = self.client.get(reverse("faq_list"), {"category": "campus", "status": "draft"})
            self.assertEqual(list(response.context["page_obj"]), [self.faq])
            for item in [self.draft, withdrawn]:
                self.assertEqual(self.client.get(reverse("faq_detail", args=[item.pk])).status_code, 404)
            self.assertContains(self.client.get(reverse("faq_detail", args=[self.faq.pk])), "到服务中心办理")

    def test_faq_category_and_answer_keyword_combination(self):
        response = self.client.get(reverse("faq_list"), {"category": "campus", "q": "服务中心"})
        self.assertEqual(list(response.context["page_obj"]), [self.faq])
        self.assertContains(response, "后勤及校园管理")
        self.assertEqual(len(self.client.get(reverse("faq_list"), {"category": "daily", "q": "服务中心"}).context["page_obj"]), 0)

    def test_admin_status_filter_and_invalid_filter(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("faq_list"), {"category": "campus", "status": "draft"})
        self.assertEqual(list(response.context["page_obj"]), [self.draft])
        for params in [{"category": "invalid"}, {"status": "invalid"}, {"q": "a" * 201}]:
            self.assertEqual(len(self.client.get(reverse("faq_list"), params).context["page_obj"]), 0)

    def test_category_links_and_pagination_keep_other_filters(self):
        self.client.force_login(self.admin)
        for index in range(14):
            FAQ.objects.create(question=f"分页问答{index}", answer="分页答案", category="daily", status="published", created_by=self.admin)
        response = self.client.get(reverse("faq_list"), {"category": "daily", "q": "分页", "status": "published", "page": 2})
        self.assertEqual(len(response.context["page_obj"]), 2)
        self.assertIn("category=daily", response.context["querystring"])
        for tab in response.context["category_tabs"]:
            self.assertIn("status=published", tab["url"])
            self.assertNotIn("page=", tab["url"])

    def test_consultation_category_required_and_saved_and_identity_cannot_be_forged(self):
        for category in ["", "invalid"]:
            response = self.client.post(reverse("consultation_create"), {"subject": "新问题", "body": "内容", "category": category})
            self.assertIn("category", response.context["form"].errors)
        response = self.client.post(reverse("consultation_create"), {"subject": "新问题", "body": "内容", "category": "party", "student": self.other.pk})
        self.assertEqual(response.status_code, 302)
        item = Conversation.objects.get(subject="新问题")
        self.assertEqual((item.category, item.student_id), ("party", self.student.pk))

    def test_consultation_keyword_message_category_waiting_filter_and_deduplication(self):
        Message.objects.create(conversation=self.thread, author=self.admin, body="独特关键词")
        Message.objects.create(conversation=self.thread, author=self.student, body="独特关键词，补充问题")
        response = self.client.get(reverse("consultation_list"), {"category": "academic", "waiting": "1", "q": "独特关键词"})
        self.assertEqual(list(response.context["page_obj"]), [self.thread])
        self.assertEqual(len(self.client.get(reverse("consultation_list"), {"waiting": "0"}).context["page_obj"]), 0)
        self.client.force_login(self.other)
        self.assertEqual(len(self.client.get(reverse("consultation_list"), {"q": "独特关键词"}).context["page_obj"]), 0)
        self.assertEqual(self.client.post(reverse("consultation_detail", args=[self.thread.pk]), {"body": "越权"}).status_code, 404)

    def test_curation_requires_admin_reply(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("consultation_detail", args=[self.thread.pk]))
        self.assertNotContains(response, "整理为官方问答")
        self.assertRedirects(self.client.get(reverse("faq_create"), {"source": self.thread.pk}), reverse("consultation_detail", args=[self.thread.pk]))
        self.assertFalse(FAQ.objects.filter(source=self.thread).exists())

    def test_curation_prefills_category_and_only_admin_answers_then_saves_draft(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("consultation_detail", args=[self.thread.pk]), {"body": "适合公开的管理员答复"})
        response = self.client.get(reverse("faq_create"), {"source": self.thread.pk})
        self.assertEqual(response.context["form"]["category"].value(), "academic")
        self.assertEqual(response.context["form"]["answer"].value(), "适合公开的管理员答复")
        self.assertNotContains(response, "私人信息不能公开")
        self.client.post(reverse("faq_create") + f"?source={self.thread.pk}", {"question": "整理后的公开问题", "answer": "整理后的公开答复", "category": "daily"})
        item = FAQ.objects.get(source=self.thread)
        self.assertEqual((item.status, item.category), ("draft", "daily"))
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("faq_detail", args=[item.pk])).status_code, 404)
        self.client.force_login(self.admin)
        self.client.post(reverse("faq_action", args=[item.pk, "publish"]))
        self.client.force_login(self.other)
        response = self.client.get(reverse("faq_detail", args=[item.pk]))
        self.assertContains(response, "整理后的公开答复")
        self.assertNotContains(response, self.thread.subject)
        self.assertNotContains(response, "私人信息不能公开")
        self.assertEqual(self.client.get(reverse("consultation_detail", args=[self.thread.pk])).status_code, 404)

    def test_students_cannot_curate_edit_or_publish_faqs(self):
        for name, args in [("faq_create", []), ("faq_edit", [self.faq.pk])]:
            self.assertEqual(self.client.get(reverse(name, args=args), {"source": self.thread.pk}).status_code, 403)
        self.assertEqual(self.client.post(reverse("faq_action", args=[self.faq.pk, "publish"])).status_code, 403)

    def test_edit_existing_faq_preserves_source_and_requires_publish_again(self):
        self.faq.source = self.thread
        self.faq.save()
        self.client.force_login(self.admin)
        self.client.post(reverse("faq_edit", args=[self.faq.pk]), {"question": "修改问题", "answer": "修改答案", "category": "awards", "source": "99999"})
        self.faq.refresh_from_db()
        self.assertEqual((self.faq.status, self.faq.category, self.faq.source_id), ("draft", "awards", self.thread.pk))

    def test_selected_category_carried_to_new_consultation_and_faq(self):
        response = self.client.get(reverse("consultation_create"), {"category": "campus"})
        self.assertEqual(response.context["form"]["category"].value(), "campus")
        self.client.force_login(self.admin)
        response = self.client.get(reverse("faq_create"), {"category": "awards"})
        self.assertEqual(response.context["form"]["category"].value(), "awards")

    def test_anonymous_cannot_read_official_or_private_content(self):
        self.client.logout()
        for name, args in [("support_home", []), ("faq_list", []), ("faq_detail", [self.faq.pk]),
                           ("consultation_list", []), ("consultation_detail", [self.thread.pk])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 302)

    def watermark_text(self, response):
        watermark=response.context["support_watermark"]
        self.assertContains(response,'class="support-watermark"')
        self.assertContains(response,'aria-hidden="true"')
        self.assertEqual(response["Cache-Control"],"private, no-store")
        root=ET.fromstring(unquote(watermark["url"].split(',',1)[1]))
        return ''.join(root.itertext())

    def test_consultation_guidance_links_on_list_create_and_invalid_submission(self):
        response=self.client.get(reverse('consultation_list'))
        self.assertContains(response,'未找到相关答案后再发起咨询')
        notice=response.content.decode().split('<aside class="consultation-guidance"')[1].split('</aside>')[0]
        for name in ['notification_list','guide_list','policy_list','faq_list']:
            self.assertIn(f'href="{reverse(name)}"',notice)
        self.assertNotContains(response,'前因后果')
        self.assertNotContains(response,'继续追问')
        self.assertIn(self.student.full_name,self.watermark_text(response))
        for response in [self.client.get(reverse('consultation_create')),
                         self.client.post(reverse('consultation_create'),{'subject':'缺少正文','category':'daily'})]:
            self.assertContains(response,'提问时，请一次性说明')
            self.assertContains(response,'前因后果')
            self.assertNotContains(response,'未找到相关答案后再发起咨询')
            self.assertNotContains(response,'继续追问')
            self.assertIn(self.student.full_name,self.watermark_text(response))
        self.assertFalse(Conversation.objects.filter(subject='缺少正文').exists())

    def test_student_watermark_covers_all_support_pages_and_filters(self):
        routes=[('faq_list',[]),('faq_detail',[self.faq.pk]),('consultation_list',[]),
                ('consultation_create',[]),('consultation_detail',[self.thread.pk])]
        for name,args in routes:
            response=self.client.get(reverse(name,args=args),{'q':'筛选','category':'campus','page':2,'full_name':'伪造姓名','username':self.other.username})
            text=self.watermark_text(response)
            self.assertIn('姓名：'+self.student.full_name,text)
            self.assertIn('学号：'+self.student.username,text)
            self.assertNotIn(self.other.username,text)
            self.assertNotIn('伪造姓名',text)
        self.client.force_login(self.other)
        text=self.watermark_text(self.client.get(reverse('faq_detail',args=[self.faq.pk])))
        self.assertIn(self.other.username,text)
        self.assertNotIn(self.student.username,text)

    def test_admin_watermark_uses_viewer_not_conversation_owner_on_all_pages(self):
        self.client.force_login(self.admin)
        routes=[('faq_list',[]),('faq_detail',[self.faq.pk]),('faq_create',[]),('faq_edit',[self.faq.pk]),
                ('consultation_list',[]),('consultation_detail',[self.thread.pk])]
        for name,args in routes:
            text=self.watermark_text(self.client.get(reverse(name,args=args)))
            self.assertIn('姓名：'+self.admin.full_name,text)
            self.assertIn('账号：'+self.admin.username,text)
            self.assertNotIn(self.student.username,text)
        response=self.client.post(reverse('faq_create'),{'question':'未填答案','category':'daily'})
        self.assertIn(self.admin.username,self.watermark_text(response))

    def test_watermark_scope_and_anonymous_identity_remain_private(self):
        for name in ['dashboard','notification_list','guide_list','policy_list']:
            self.assertNotContains(self.client.get(reverse(name)),'class="support-watermark"')
        self.client.logout()
        for name,args in [('faq_list',[]),('faq_detail',[self.faq.pk]),('consultation_list',[]),('consultation_create',[])]:
            response=self.client.get(reverse(name,args=args))
            self.assertEqual(response.status_code,302)
            self.assertNotIn(b'data:image/svg+xml',response.content)

    def test_watermark_escapes_special_names_and_preserves_long_identity(self):
        regular=support_watermark(SimpleNamespace(full_name='普通同学',username='20260001',is_staff=False))
        self.assertLess(regular['width']*regular['height'],360*190)
        name='王<同学&"名字"><script>alert(1)</script>'
        user=SimpleNamespace(full_name=name,username='001234',is_staff=False)
        svg=unquote(support_watermark(user)['url'].split(',',1)[1])
        root=ET.fromstring(svg)
        self.assertIn(name,''.join(root.itertext()))
        self.assertFalse(root.findall('.//{http://www.w3.org/2000/svg}script'))
        user.full_name='姓名'*40
        user.username='0'*150
        data=support_watermark(user)
        text=''.join(ET.fromstring(unquote(data['url'].split(',',1)[1])).itertext())
        self.assertIn(user.full_name,text)
        self.assertIn(user.username,text)
        self.assertGreater(data['height'],190)
        user.full_name=''
        self.assertIn('姓名：未填写',unquote(support_watermark(user)['url']))
        user.full_name='测试\x00\ufffe\ud800姓名'
        root=ET.fromstring(unquote(support_watermark(user)['url'].split(',',1)[1]))
        self.assertIn('测试 姓名',''.join(root.itertext()))

    def test_students_can_follow_up_after_each_reply_and_waiting_state_reopens(self):
        url=reverse('consultation_detail',args=[self.thread.pk])
        before=self.client.get(url)
        self.assertNotContains(before,'继续追问')
        self.assertNotContains(before,'class="reply-hint"')
        self.assertEqual(before.context['form'].fields['body'].label,'补充说明')
        self.assertRedirects(self.client.post(url,{'body':'管理员回复前先补充情况'}),url)
        self.assertNotContains(self.client.get(url),'继续追问')
        self.client.force_login(self.admin)
        self.assertRedirects(self.client.post(url,{'body':'首次管理员回复'}),url)
        self.thread.refresh_from_db();self.assertFalse(self.thread.awaiting_reply)
        for index in range(2):
            self.client.force_login(self.student)
            page=self.client.get(url)
            self.assertContains(page,'继续追问')
            self.assertContains(page,'发送追问 / 补充')
            self.assertRedirects(self.client.post(url,{'body':f'针对原问题追问{index}'}),url)
            self.thread.refresh_from_db();self.assertTrue(self.thread.awaiting_reply)
            self.assertContains(self.client.get(url),'如对管理员回复仍有疑问')
            invalid=self.client.post(url,{'body':''})
            self.assertIn('body',invalid.context['form'].errors)
            self.assertIn(self.student.username,self.watermark_text(invalid))
            self.client.force_login(self.other)
            self.assertEqual(self.client.post(url,{'body':'不能参与别人的会话'}).status_code,404)
            self.client.force_login(self.admin)
            self.assertIn(self.thread,list(self.client.get(reverse('consultation_list'),{'waiting':'1'}).context['page_obj']))
            self.assertRedirects(self.client.post(url,{'body':f'继续解答{index}'}),url)
            self.thread.refresh_from_db();self.assertFalse(self.thread.awaiting_reply)
        self.assertEqual(self.thread.messages.count(),7)
        self.assertEqual(Conversation.objects.filter(student=self.student).count(),1)
