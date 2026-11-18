from datetime import date, timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from .content_order import CONTENT_MODULES
from .models import (AttendanceBatch, Audit, CadreTenure, Conversation, FAQ, Leave, Message, MonthlyAttendance,
    Notification, Policy, PolicyRevision, Registration, ServiceGuide, StudentImportBatch, StudentRosterBatch, User, Video)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ContentOrderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin=User.objects.create_user(username="order-admin",is_staff=True,full_name="排序管理员")
        cls.other=User.objects.create_user(username="order-other",is_staff=True)
        cls.student=User.objects.create_user(username="order-student",full_name="排序学生")
        cls.items={}
        for kind,(model,_,_) in CONTENT_MODULES.items():
            rows=[]
            for index in range(3):
                if kind=='notification':item=model.objects.create(title=f"通知{index}",body="正文",category='party' if index!=1 else 'other',created_by=cls.admin)
                elif kind=='guide':item=model.objects.create(title=f"指南{index}",owner="老师",contact="测试",office="测试地点",process="第一步",created_by=cls.admin)
                elif kind=='faq':item=model.objects.create(question=f"问题{index}",answer="答复",status='published',created_by=cls.admin)
                elif kind=='video':item=model.objects.create(title=f"视频{index}",file='test.mp4',uploaded_by=cls.admin)
                else:
                    item=model.objects.create(status='published',created_by=cls.admin)
                    rev=PolicyRevision.objects.create(policy=item,title=f"制度{index}",category='daily',is_draft=False)
                    item.published_revision=rev;item.save()
                rows.append(item)
            cls.items[kind]=rows

    def setUp(self):self.client.force_login(self.admin)

    def token(self,kind,pk,params=None):
        response=self.client.get(reverse(CONTENT_MODULES[kind][1]),params or {})
        item=next(item for item in response.context['page_obj'] if item.pk==pk)
        return item.order_controls['token']

    def move(self,kind,item,move='top',token=None,return_to=None):
        return self.client.post(reverse('content_order',args=[kind,item.pk]),{'move':move,'order_token':token or self.token(kind,item.pk),
            'return_to':return_to or reverse(CONTENT_MODULES[kind][1])})

    def test_all_five_modules_reorder_for_admin_and_student_without_changing_content_dates(self):
        for kind,(model,list_name,_) in CONTENT_MODULES.items():
            with self.subTest(module=kind):
                self.client.force_login(self.admin)
                item=self.items[kind][0];before=model.objects.values().get(pk=item.pk)
                self.assertEqual(self.move(kind,item).status_code,302)
                self.assertEqual(model.objects.first().pk,item.pk)
                after=model.objects.values().get(pk=item.pk)
                self.assertEqual({k:v for k,v in before.items() if k!='display_order'},{k:v for k,v in after.items() if k!='display_order'})
                self.client.force_login(self.student)
                response=self.client.get(reverse(list_name))
                self.assertEqual(response.context['page_obj'][0].pk,item.pk)
                self.assertNotContains(response,'调整展示顺序')

    def test_all_move_directions_and_new_content_at_end(self):
        items=self.items['notification'];model=Notification
        self.move('notification',items[0],'top')
        self.move('notification',items[0],'down')
        self.assertEqual(list(model.objects.values_list('pk',flat=True)).index(items[0].pk),1)
        self.move('notification',items[0],'up');self.assertEqual(model.objects.first().pk,items[0].pk)
        self.move('notification',items[0],'bottom');self.assertEqual(list(model.objects.all())[-1].pk,items[0].pk)
        new=model.objects.create(title='新内容',body='正文',created_by=self.admin)
        self.assertEqual(list(model.objects.all())[-1],new)

    def test_category_scope_and_cross_page_neighbor_are_preserved(self):
        item=self.items['notification'][0]
        params={'category':'party','per_page':1,'page':2}
        token=self.token('notification',item.pk,params)
        response=self.move('notification',item,'up',token,reverse('notification_list')+'?category=party&per_page=1&page=2')
        query=parse_qs(urlsplit(response.url).query)
        self.assertEqual(query['category'],['party']);self.assertEqual(query['page'],['1']);self.assertEqual(query['per_page'],['1'])
        self.assertEqual(Notification.objects.filter(category='party').first(),item)

    def test_stale_or_other_actor_tokens_do_not_overwrite_order(self):
        item=self.items['faq'][0];token=self.token('faq',item.pk)
        self.move('faq',item,'top',token)
        before=list(FAQ.objects.values_list('pk','display_order'))
        response=self.move('faq',item,'bottom',token)
        self.assertContains(self.client.get(response.url),'已变化')
        self.assertEqual(list(FAQ.objects.values_list('pk','display_order')),before)
        fresh=self.token('faq',item.pk);self.client.force_login(self.other)
        self.move('faq',item,'bottom',fresh)
        self.assertEqual(list(FAQ.objects.values_list('pk','display_order')),before)

    def test_permissions_csrf_invalid_token_and_safe_return_url(self):
        item=self.items['notification'][0];url=reverse('content_order',args=['notification',item.pk]);token=self.token('notification',item.pk)
        self.assertEqual(self.client.get(url).status_code,405)
        client=Client(enforce_csrf_checks=True);client.force_login(self.admin)
        self.assertEqual(client.post(url,{'move':'top','order_token':token}).status_code,403)
        self.client.force_login(self.student);self.assertEqual(self.client.post(url,{'move':'top','order_token':token}).status_code,403)
        self.client.logout();self.assertEqual(self.client.post(url,{}).status_code,302)
        self.client.force_login(self.admin)
        before=list(Notification.objects.values_list('pk','display_order'))
        self.client.post(url,{'move':'top','order_token':'forged'})
        self.assertEqual(list(Notification.objects.values_list('pk','display_order')),before)
        response=self.move('notification',item,'top',token,'https://example.invalid/away')
        self.assertTrue(response.url.startswith('/notifications/'))

    def test_order_audit_failure_rolls_back(self):
        item=self.items['guide'][0];token=self.token('guide',item.pk)
        before=list(ServiceGuide.objects.values_list('pk','display_order'))
        with patch('portal.content_order.audit',side_effect=IntegrityError('audit failure')),self.assertRaises(IntegrityError):
            self.move('guide',item,'top',token)
        self.assertEqual(list(ServiceGuide.objects.values_list('pk','display_order')),before)

    def test_ordering_never_exposes_withdrawn_drafts_or_deleted_resources(self):
        Notification.objects.filter(pk=self.items['notification'][0].pk).update(status='withdrawn')
        ServiceGuide.objects.filter(pk=self.items['guide'][0].pk).update(status='withdrawn')
        FAQ.objects.filter(pk=self.items['faq'][0].pk).update(status='draft')
        Policy.objects.filter(pk=self.items['policy'][0].pk).update(status='draft')
        Video.objects.filter(pk=self.items['video'][0].pk).update(is_deleted=True)
        self.client.force_login(self.student)
        for kind,(_,name,_) in CONTENT_MODULES.items():
            self.assertNotIn(self.items[kind][0].pk,[item.pk for item in self.client.get(reverse(name)).context['page_obj']])


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class HomeAndTableSortTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin=User.objects.create_user(username='metrics-admin',full_name='管理员',is_staff=True)
        cls.student=User.objects.create_user(username='0002',full_name='B',student_class='B班')
        cls.other=User.objects.create_user(username='0010',full_name='A',student_class='A班')
        now=timezone.now()
        cls.threads=[]
        for user,category,waiting in [(cls.student,'daily',True),(cls.student,'party',True),(cls.other,'academic',False)]:
            thread=Conversation.objects.create(student=user,category=category,subject='私有咨询'+user.username,awaiting_reply=waiting)
            Message.objects.create(conversation=thread,author=user,body='问题')
            cls.threads.append(thread)
        Message.objects.create(conversation=cls.threads[0],author=cls.admin,body='已回复后仍有追问')
        Message.objects.create(conversation=cls.threads[2],author=cls.admin,body='答复')
        for user,kind,status in [(cls.student,'sick','pending'),(cls.other,'personal','approved'),(cls.student,'other','closed')]:
            Leave.objects.create(student=user,student_name=user.full_name,student_number=user.username,kind=kind,status=status,
                starts_at=now,ends_at=now+timedelta(days=1),reason='测试',destination='测试',contact='测试')
        for kind in ['internship','rental']:
            for user in [cls.student,cls.other]:
                Registration.objects.create(student=user,student_name=user.full_name,student_number=user.username,kind=kind,
                    starts_at=now,ends_at=now+timedelta(days=1),address='测试',contact='测试',employer='企业'+user.full_name,
                    emergency_name='测试',emergency_contact='测试',rental_reason='research' if kind=='rental' else '')
        Notification.objects.create(title='公开通知',category='party',body='正文',created_by=cls.admin)
        Notification.objects.create(title='撤下通知',category='other',body='私有',status='withdrawn',created_by=cls.admin)

    def setUp(self):self.client.force_login(self.admin)

    def test_home_reply_rate_counts_prior_reply_even_when_followup_waits(self):
        response=self.client.get(reverse('dashboard'))
        self.assertEqual(response.context['consultation_total'],3)
        self.assertEqual(response.context['consultation_answered'],2)
        self.assertEqual(response.context['reply_rate'],'66.7')
        self.assertEqual(response.context['consult_count'],2)
        self.assertContains(response,'首页');self.assertNotContains(response,'工作台')
        self.assertEqual(response.context['notification_distribution']['total'],1)

    def test_home_student_scope_and_zero_data(self):
        self.client.force_login(self.student);response=self.client.get(reverse('dashboard'))
        self.assertEqual(response.context['consultation_total'],2)
        self.assertEqual(response.context['reply_rate'],'50.0')
        self.assertNotContains(response,'私有咨询'+self.other.username)
        self.assertNotContains(response,'撤下通知')
        self.assertEqual(len(response.context['consultation_trend']),6)
        empty=User.objects.create_user(username='empty',full_name='空数据同学');self.client.force_login(empty)
        response=self.client.get(reverse('dashboard'))
        self.assertEqual(response.context['consultation_total'],0)
        self.assertEqual(response.context['consultation_distribution']['gradient'],'#edf1ed')

    def test_business_tables_sort_entire_queryset_before_paging_and_preserve_filters(self):
        for name,key in [('leave_list','number'),('internship_list','name'),('rental_list','name'),('account_list','full_name')]:
            with self.subTest(page=name):
                response=self.client.get(reverse(name),{'sort':key,'direction':'asc','per_page':1,'page':1})
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.context['page_obj'].paginator.per_page,1)
                if name == 'account_list':
                    self.assertNotContains(response,'sort-triangle')
                    self.assertContains(response,'data-column-resizer="identity"')
                    self.assertContains(response,'学号 / 姓名')
                else:
                    self.assertContains(response,'sort-triangle asc')
                self.assertIn('sort='+key,response.context['querystring'])
                self.assertContains(response,f'name="sort" value="{key}"')
        students=self.client.get(reverse('account_list'),{'sort':'full_name','direction':'asc','per_page':100}).context['page_obj']
        self.assertEqual([item.full_name for item in students],['A','B'])

    def test_student_cannot_sort_into_other_students_records(self):
        self.client.force_login(self.student)
        for name in ['leave_list','internship_list','rental_list']:
            page=self.client.get(reverse(name),{'sort':'name','direction':'desc'}).context['page_obj']
            self.assertTrue(all(item.student_id==self.student.pk for item in page))

    def test_invalid_sort_is_safe_and_export_uses_full_sorted_results(self):
        response=self.client.get(reverse('leave_list'),{'sort':'__class__','direction':'desc'})
        self.assertEqual(len(response.context['page_obj']),0)
        self.assertContains(response,'排序参数无效')
        self.assertEqual(self.client.get(reverse('leave_export'),{'sort':'forged'}).status_code,400)
        import csv,io
        response=self.client.get(reverse('leave_export'),{'sort':'id','direction':'asc','per_page':1})
        rows=list(csv.DictReader(io.StringIO(b''.join(response.streaming_content).decode('utf-8-sig'))))
        self.assertEqual(len(rows),3)
        self.assertEqual([int(row['申请编号']) for row in rows],sorted(int(row['申请编号']) for row in rows))

    def test_preview_numeric_sorting_is_global_and_payload_remains_unchanged(self):
        payload=[{'username':str(index),'name':'测试','month':'2026-11','clock_days':days,'leave_days':0} for index,days in enumerate([20,2,10])]
        batch=AttendanceBatch.objects.create(uploaded_by=self.admin,row_count=3,payload=payload)
        response=self.client.get(reverse('attendance_preview',args=[batch.pk]),{'sort':'clock_days','direction':'asc','per_page':1,'page':2})
        self.assertEqual(response.context['rows'][0]['clock_days'],10)
        batch.refresh_from_db();self.assertEqual(batch.payload,payload)

    def test_remaining_preview_history_and_audit_headers_sort(self):
        CadreTenure.objects.create(student=self.student,position='B职务',starts_on=date(2025,1,1))
        CadreTenure.objects.create(student=self.student,position='A职务',starts_on=date(2026,1,1))
        response=self.client.get(reverse('account_detail',args=[self.student.pk]),{'sort':'position'})
        self.assertEqual(response.context['tenures'][0].position,'A职务')
        Audit.objects.create(actor=self.admin,action='Z操作',target='1');Audit.objects.create(actor=self.admin,action='A操作',target='2')
        self.assertEqual(self.client.get(reverse('audit_list'),{'sort':'action'}).context['page_obj'][0].action,'A操作')
        batch=StudentImportBatch.objects.create(uploaded_by=self.admin,row_count=2,payload=[{'username':'2','full_name':'B'},{'username':'1','full_name':'A'}])
        self.assertEqual(self.client.get(reverse('account_preview',args=[batch.pk]),{'sort':'full_name'}).context['rows'][0]['full_name'],'A')
        roster=StudentRosterBatch.objects.create(uploaded_by=self.admin,kind='tenure',row_count=2,payload=[{'username':'2','name':'B','position':'B'},{'username':'1','name':'A','position':'A'}])
        self.assertEqual(self.client.get(reverse('roster_preview',args=[roster.pk]),{'sort':'position'}).context['page_obj'][0]['position'],'A')

    def test_guides_use_directory_and_chat_has_distinct_role_styles(self):
        response=self.client.get(reverse('guide_list'))
        self.assertContains(response,'guide-directory')
        self.assertNotContains(response,'class="card-grid"')
        response=self.client.get(reverse('consultation_detail',args=[self.threads[0].pk]))
        self.assertContains(response,'student-message');self.assertContains(response,'staff-message')
        self.assertContains(response,'教师 / 管理员');self.assertContains(response,'message-role')
