from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch
from django.db import DatabaseError
from django.db.models import Sum
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from .analytics import aggregate_metrics, touch_activity
from .models import ContentVisitDaily, FAQ, Notification, Policy, PolicyRevision, ServiceGuide, StudentDailyActivity, User, Video
from .student_services import delete_students, student_identity_digest
from .student_state import student_edit_digest


NOW = datetime(2026, 11, 18, 4, tzinfo=dt_timezone.utc)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class StudentAnalyticsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(username='analytics-admin', full_name='统计管理员', is_staff=True)
        cls.student = User.objects.create_user(username='analytics-001', full_name='活跃同学', password='TestStudent!2026')
        cls.other = User.objects.create_user(username='analytics-002', full_name='另一位同学')
        cls.faq = FAQ.objects.create(question='统计测试问题', answer='答案', status='published', created_by=cls.admin)
        cls.notification = Notification.objects.create(title='统计测试通知', body='正文', created_by=cls.admin)
        cls.guide = ServiceGuide.objects.create(title='统计测试流程', owner='老师', contact='联系', office='地点', process='流程', created_by=cls.admin)
        cls.video = Video.objects.create(title='统计测试视频', file='missing.mp4', uploaded_by=cls.admin)
        cls.policy = Policy.objects.create(status='published', created_by=cls.admin)
        cls.revision = PolicyRevision.objects.create(policy=cls.policy, title='统计测试制度', category='daily', is_draft=False)
        cls.policy.published_revision = cls.revision
        cls.policy.save()

    def setUp(self):
        self.clock = patch('portal.analytics.timezone.now', return_value=NOW)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.client.force_login(self.student)

    def views(self):
        return ContentVisitDaily.objects.aggregate(n=Sum('views'))['n'] or 0

    def test_each_authorized_detail_counts_only_student_and_displays_metrics_on_both_pages(self):
        for kind, item in [('notification', self.notification), ('policy', self.policy), ('faq', self.faq), ('guide', self.guide), ('video', self.video)]:
            with self.subTest(kind=kind):
                response = self.client.get(reverse(kind+'_detail', args=[item.pk]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['content_metrics'], {'total': 1, 'month': 1, 'today': 1})
                listing = self.client.get(reverse(kind+'_list'))
                self.assertEqual(listing.context['page_obj'][0].content_metrics['total'], 1)
                self.assertContains(listing, '总点击量')
        self.assertEqual(self.views(), 5)
        self.client.force_login(self.admin)
        for kind, item in [('notification', self.notification), ('policy', self.policy), ('faq', self.faq), ('guide', self.guide), ('video', self.video)]:
            self.client.get(reverse(kind+'_detail', args=[item.pk]))
        self.assertEqual(self.views(), 5)
        self.assertFalse(StudentDailyActivity.objects.filter(student=self.admin).exists())

    def test_every_repeated_click_counts_across_sessions_without_a_time_limit(self):
        path = reverse('faq_detail', args=[self.faq.pk])
        self.client.get(path); self.client.get(path)
        other_session = Client(); other_session.force_login(self.student); other_session.get(path)
        self.assertEqual(self.views(), 3)
        self.client.force_login(self.other); self.client.get(path)
        self.assertEqual(self.views(), 4)
        with patch('portal.analytics.timezone.now', return_value=NOW+timedelta(seconds=61)):
            other_session.get(path)
        self.assertEqual(self.views(), 5)
        self.assertEqual(ContentVisitDaily.objects.count(), 2)

    def test_denied_head_prefetch_list_and_file_requests_do_not_inflate_counts(self):
        path = reverse('faq_detail', args=[self.faq.pk])
        self.client.head(path); self.client.get(path, HTTP_SEC_PURPOSE='prefetch;prerender')
        self.assertEqual(self.views(), 0)
        self.assertEqual(StudentDailyActivity.objects.count(), 0)
        self.client.get(reverse('faq_list'))
        self.client.get(reverse('video_file', args=[self.video.pk]), HTTP_RANGE='bytes=0-3')
        FAQ.objects.filter(pk=self.faq.pk).update(status='withdrawn')
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.views(), 0)

    def test_all_withdrawn_or_deleted_content_is_inaccessible_and_not_counted(self):
        Notification.objects.filter(pk=self.notification.pk).update(status='withdrawn')
        Policy.objects.filter(pk=self.policy.pk).update(status='withdrawn')
        FAQ.objects.filter(pk=self.faq.pk).update(status='draft')
        ServiceGuide.objects.filter(pk=self.guide.pk).update(status='withdrawn')
        Video.objects.filter(pk=self.video.pk).update(is_deleted=True)
        for kind, item in [('notification', self.notification), ('policy', self.policy), ('faq', self.faq), ('guide', self.guide), ('video', self.video)]:
            self.assertEqual(self.client.get(reverse(kind+'_detail', args=[item.pk])).status_code, 404)
        self.assertEqual(self.views(), 0)
        self.assertEqual(StudentDailyActivity.objects.count(), 0)

    def test_date_boundaries_aggregates_and_future_rows(self):
        today = timezone.localdate(NOW)
        for day, views in [(today, 3), (today.replace(day=1), 4), (today.replace(month=10, day=31), 5), (today+timedelta(days=1), 90)]:
            ContentVisitDaily.objects.create(student=self.student, kind='faq', object_id=self.faq.pk, day=day, views=views)
        self.assertEqual(aggregate_metrics(ContentVisitDaily.objects.all()), {'total': 12, 'month': 7, 'today': 3})
        self.client.force_login(self.admin)
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.context['click_metrics'], {'total': 12, 'month': 7, 'today': 3})

    def test_home_clicks_are_private_to_student_and_all_for_admin(self):
        day = timezone.localdate(NOW)
        ContentVisitDaily.objects.create(student=self.student, kind='faq', object_id=self.faq.pk, day=day, views=3)
        ContentVisitDaily.objects.create(student=self.other, kind='guide', object_id=self.guide.pk, day=day, views=8)
        ContentVisitDaily.objects.create(student=self.student, kind='notification', object_id=self.notification.pk, day=day, views=5)
        ContentVisitDaily.objects.create(student=self.other, kind='notification', object_id=self.notification.pk, day=day, views=7)
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.context['click_metrics']['today'], 8)
        self.assertEqual(next(row['today'] for row in response.context['click_breakdown'] if row['key']=='notification'), 5)
        self.assertEqual(len(response.context['click_breakdown']), 5)
        self.client.force_login(self.admin)
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.context['click_metrics']['today'], 23)
        for period in ['total', 'month', 'today']:
            self.assertEqual(sum(row[period] for row in response.context['click_breakdown']), response.context['click_metrics'][period])

    def test_notification_repeats_flow_into_student_activity_and_export_totals(self):
        for _ in range(3):
            self.client.get(reverse('notification_detail', args=[self.notification.pk]))
        self.client.force_login(self.admin)
        response = self.client.get(reverse('account_detail', args=[self.student.pk]))
        data = response.context['student_activity']
        self.assertEqual(data['clicks'], 3)
        self.assertEqual(next(row['count'] for row in data['kinds'] if row['label']=='通知汇总'), 3)
        self.assertEqual(len(data['kinds']), 5)
        import io
        from openpyxl import load_workbook
        response = self.client.get(reverse('account_export'), {'q': self.student.username})
        raw = b''.join(response.streaming_content); response.close()
        book = load_workbook(io.BytesIO(raw), read_only=True)
        self.assertEqual(book['学生信息明细']['S6'].value, 3)
        self.assertEqual(book['学生信息明细']['T6'].value, 3)
        book.close()

    def test_midnight_creates_new_activity_day_even_within_write_interval(self):
        before = datetime(2026, 11, 18, 15, 59, 45, tzinfo=dt_timezone.utc)
        after = before+timedelta(seconds=25)
        touch_activity(self.student, before); touch_activity(self.student, after)
        self.assertEqual(StudentDailyActivity.objects.filter(student=self.student).count(), 2)
        self.student.refresh_from_db(); self.assertEqual(self.student.last_seen_at, after)

    def test_click_month_rollover_uses_beijing_midnight_not_utc_midnight(self):
        before = datetime(2026, 11, 30, 15, 59, 45, tzinfo=dt_timezone.utc)
        path = reverse('faq_detail', args=[self.faq.pk])
        with patch('portal.analytics.timezone.now', return_value=before):
            self.client.force_login(self.student)
            self.client.get(path)
        with patch('portal.analytics.timezone.now', return_value=before+timedelta(seconds=25)):
            response = self.client.get(path)
        self.assertEqual(response.context['content_metrics'], {'total': 2, 'month': 1, 'today': 1})
        self.assertEqual(StudentDailyActivity.objects.filter(student=self.student).count(), 2)

    def test_activity_writes_throttled_without_changing_edit_version(self):
        before = student_edit_digest(self.student)
        touch_activity(self.student, NOW)
        self.student.refresh_from_db(); first = self.student.last_seen_at
        with self.assertNumQueries(0):
            touch_activity(self.student, NOW+timedelta(seconds=30))
        self.student.refresh_from_db(); self.assertEqual(self.student.last_seen_at, first)
        touch_activity(self.student, NOW+timedelta(seconds=61))
        self.student.refresh_from_db(); self.assertEqual(self.student.last_seen_at, NOW+timedelta(seconds=61))
        self.assertEqual(student_edit_digest(self.student), before)
        self.assertEqual(StudentDailyActivity.objects.count(), 1)

    def test_presence_color_precedence_and_expiry(self):
        self.assertEqual(self.student.presence['key'], 'offline')
        touch_activity(self.student); self.student.refresh_from_db()
        self.assertEqual(self.student.presence['key'], 'online')
        with patch('portal.analytics.timezone.now', return_value=NOW+timedelta(minutes=5)):
            self.assertEqual(self.student.presence['key'], 'offline')
        self.student.enrollment_status = 'graduated'; self.assertEqual(self.student.presence['key'], 'restricted')
        self.student.enrollment_status = 'enrolled'; self.student.is_active = False
        self.assertEqual(self.student.presence['label'], '账号已停用')

    def test_logout_expires_presence_but_preserves_last_activity(self):
        self.client.get(reverse('dashboard'))
        self.student.refresh_from_db(); seen = self.student.last_seen_at
        self.client.post(reverse('logout'))
        self.student.refresh_from_db()
        self.assertEqual(self.student.presence['key'], 'offline')
        self.assertEqual(self.student.last_seen_at, seen)

    def test_activity_ping_requires_student_post_csrf_and_does_not_count_content(self):
        path = reverse('activity_ping')
        self.assertEqual(self.client.get(path).status_code, 405)
        guarded = Client(enforce_csrf_checks=True); guarded.force_login(self.student)
        self.assertEqual(guarded.post(path).status_code, 403)
        self.assertEqual(self.client.post(path).status_code, 200)
        self.assertEqual(StudentDailyActivity.objects.count(), 1)
        self.assertEqual(self.views(), 0)
        self.client.force_login(self.admin); self.assertEqual(self.client.post(path).status_code, 403)
        self.client.logout(); self.assertEqual(self.client.post(path).status_code, 302)

    def test_presence_api_is_staff_only_bounded_and_private(self):
        path = reverse('account_presence')
        self.assertEqual(self.client.get(path, {'ids': self.student.pk}).status_code, 403)
        self.client.force_login(self.admin)
        for values in [[], ['invalid'], ['-1'], ['1']*101]:
            self.assertEqual(self.client.get(path, {'ids': values}).status_code, 400)
        response = self.client.get(path, {'ids': [self.student.pk, self.admin.pk]})
        self.assertEqual(set(response.json()['statuses']), {str(self.student.pk)})
        self.assertEqual(set(response.json()['statuses'][str(self.student.pk)]), {'key', 'label'})
        self.assertEqual(response['Cache-Control'], 'private, no-store')

    def test_monthly_activity_rate_calendar_and_kind_counts(self):
        month = timezone.localdate(NOW).replace(day=1)
        StudentDailyActivity.objects.bulk_create([StudentDailyActivity(student=self.student, day=month.replace(day=day)) for day in [1, 2, 3, 18]])
        ContentVisitDaily.objects.create(student=self.student, kind='faq', object_id=self.faq.pk, day=month, views=7)
        self.client.force_login(self.admin)
        response = self.client.get(reverse('account_detail', args=[self.student.pk]), {'month': '2026-11'})
        data = response.context['student_activity']
        self.assertEqual((data['days'], data['elapsed'], data['rate'], data['label'], data['clicks']), (4, 18, 22.2, '低活跃', 7))
        self.assertEqual(sum(day['active'] for day in data['calendar']), 4)
        self.assertEqual(sum(day['future'] for day in data['calendar']), 12)
        self.assertContains(response, '月度活跃情况')
        self.assertContains(response, 'data-student-presence')
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('account_detail', args=[self.student.pk])).status_code, 403)

    def test_past_month_and_invalid_month_do_not_silently_show_other_data(self):
        StudentDailyActivity.objects.create(student=self.student, day=timezone.localdate(NOW).replace(month=10, day=1))
        self.client.force_login(self.admin)
        path = reverse('account_detail', args=[self.student.pk])
        response = self.client.get(path, {'month': '2026-10'})
        self.assertEqual(response.context['student_activity']['elapsed'], 31)
        for month in ['2026-12', 'garbage', '10000-01']:
            response = self.client.get(path, {'month': month})
            self.assertFalse(response.context['student_activity']['valid'])
            self.assertContains(response, 'notice error')

    def test_click_failure_rolls_back_without_breaking_content(self):
        original = ContentVisitDaily.objects.filter
        def fail_write(*args, **kwargs):
            if 'pk' in kwargs:
                raise DatabaseError('test failure')
            return original(*args, **kwargs)
        with patch('portal.analytics.ContentVisitDaily.objects.filter', side_effect=fail_write):
            response = self.client.get(reverse('faq_detail', args=[self.faq.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContentVisitDaily.objects.count(), 0)

    def test_activity_history_protects_student_deletion(self):
        touch_activity(self.student)
        self.student.refresh_from_db()
        deleted, archived = delete_students([self.student.pk], self.admin, student_identity_digest([self.student]))
        self.assertEqual((deleted, archived), (0, 1))
        self.assertEqual(StudentDailyActivity.objects.filter(student=self.student).count(), 1)
        self.student.refresh_from_db(); self.assertEqual(self.student.presence['key'], 'restricted')
