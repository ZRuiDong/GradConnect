import io
from datetime import date, timedelta
from unittest.mock import patch
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook
from .models import AttendanceBatch, Audit, CadreTenure, ContentVisitDaily, MonthlyAttendance, StudentDailyActivity, User


def workbook(response):
    data = b''.join(response.streaming_content)
    response.close()
    return load_workbook(io.BytesIO(data), data_only=False)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class DetailedReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(username='report-manager', full_name='报表管理员', is_staff=True)
        cls.student = User.objects.create_user(username='000001', full_name='甲同学', id_number='990000200001010001',
            student_class='一班', party_branch='第一党支部', political_status='league')
        cls.other = User.objects.create_user(username='000002', full_name='乙同学', student_class='二班')
        cls.batch = AttendanceBatch.objects.create(uploaded_by=cls.admin, is_confirmed=True, row_count=3)
        cls.month = timezone.localdate().replace(day=1)
        for student, clock in [(cls.student, 20), (cls.other, 10)]:
            MonthlyAttendance.objects.create(student=student, student_name=student.full_name, student_number=student.username,
                month=cls.month, clock_days=clock, leave_days=2, batch=cls.batch)
        MonthlyAttendance.objects.create(student=cls.student, student_name='历史姓名', student_number='000001',
            month=date(2025, 1, 1), clock_days=3, leave_days=1, batch=cls.batch)
        CadreTenure.objects.create(student=cls.student, position='班长', starts_on=date(2020, 1, 1))
        CadreTenure.objects.create(student=cls.student, position='历史委员', starts_on=date(2018, 1, 1), ends_on=date(2019, 1, 1))
        StudentDailyActivity.objects.create(student=cls.student, day=cls.month)
        ContentVisitDaily.objects.create(student=cls.student, kind='faq', object_id=1, day=cls.month, views=5)

    def setUp(self):self.client.force_login(self.admin)

    def test_attendance_exports_all_filtered_rows_and_monthly_summary_not_just_page(self):
        response = self.client.get(reverse('attendance_export'), {'month_from': self.month.strftime('%Y-%m'),
            'month_to': self.month.strftime('%Y-%m'), 'per_page': 1, 'page': 2, 'sort': 'clock_days', 'direction': 'desc'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        book = workbook(response)
        self.assertEqual(book.sheetnames, ['报表汇总', '按月汇总', '月度打卡明细'])
        detail = book['月度打卡明细']
        self.assertEqual(detail.max_row, 7)
        self.assertEqual([detail.cell(row, 6).value for row in [6, 7]], [20, 10])
        self.assertEqual(detail['A6'].value, '000001')
        self.assertEqual(detail['A6'].data_type, 's')
        self.assertEqual(book['按月汇总']['F6'].value, 34)
        self.assertEqual(detail.freeze_panes, 'A6')
        self.assertEqual(detail.auto_filter.ref, 'A5:R7')
        self.assertTrue(Audit.objects.filter(action='导出月度打卡报表').exists())

    def test_attendance_export_range_day_filters_and_legacy_links_match_list(self):
        params = {'month_from': '2025-01', 'month_to': self.month.strftime('%Y-%m'), 'day_kind': 'total_days', 'days_min': 20,
                  'q': '000001'}
        listed = self.client.get(reverse('attendance_list'), params).context['page_obj'].paginator.count
        book = workbook(self.client.get(reverse('attendance_export'), params))
        self.assertEqual(book['月度打卡明细'].max_row-5, listed)
        self.assertEqual(listed, 1)
        book = workbook(self.client.get(reverse('attendance_export'), {'year': 2025, 'month': 1}))
        self.assertEqual(book['月度打卡明细']['B6'].value, '历史姓名')

    def test_students_can_export_only_their_own_attendance_and_not_profiles(self):
        self.client.force_login(self.student)
        book = workbook(self.client.get(reverse('attendance_export'), {'year': self.month.year, 'month': '', 'q': self.other.username}))
        self.assertEqual({row[0].value for row in list(book['月度打卡明细'].rows)[5:]}, {self.student.username})
        self.assertEqual(self.client.get(reverse('account_export')).status_code, 403)

    def test_student_report_has_full_identity_current_roles_history_and_activity(self):
        book = workbook(self.client.get(reverse('account_export'), {'student_class': ['一班'], 'per_page': 1, 'page': 9}))
        self.assertEqual(book.sheetnames, ['报表汇总', '学生信息明细', '干部履职历史'])
        detail = book['学生信息明细']
        self.assertEqual(detail.max_row, 6)
        self.assertEqual(detail['A6'].value, '000001')
        self.assertEqual(detail['C6'].value, '990000200001010001')
        self.assertEqual(detail['C6'].data_type, 's')
        self.assertEqual(detail['K6'].value, '班长')
        self.assertEqual(detail['L6'].value, 2)
        self.assertEqual(detail['Q6'].value, 1)
        self.assertEqual(detail['S6'].value, 5)
        self.assertEqual(book['干部履职历史'].max_row, 7)
        self.assertEqual(book['干部履职历史']['F6'].value, '是')
        self.assertEqual(book['干部履职历史']['F7'].value, '否')

    def test_text_cells_cannot_become_formulas_or_lose_identifiers(self):
        User.objects.filter(pk=self.student.pk).update(full_name='=1+1', party_branch='@SUM(A1:A2)')
        book = workbook(self.client.get(reverse('account_export'), {'q': self.student.username}))
        self.assertEqual(book['学生信息明细']['B6'].value, '=1+1')
        self.assertEqual(book['学生信息明细']['B6'].data_type, 's')
        self.assertEqual(book['学生信息明细']['E6'].data_type, 's')

    def test_invalid_filters_empty_results_permissions_and_methods(self):
        self.assertEqual(self.client.get(reverse('attendance_export'), {'month_from': 'invalid'}).status_code, 400)
        self.assertEqual(self.client.get(reverse('account_export'), {'student_class': 'forged'}).status_code, 400)
        self.assertEqual(self.client.post(reverse('account_export')).status_code, 405)
        book = workbook(self.client.get(reverse('account_export'), {'q': 'no-such-student'}))
        self.assertEqual(book['学生信息明细'].max_row, 5)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('attendance_export')).status_code, 302)
        self.assertEqual(self.client.get(reverse('account_export')).status_code, 302)

    def test_home_student_card_shows_only_own_current_roles(self):
        CadreTenure.objects.create(student=self.student, position='团支书', starts_on=self.month)
        CadreTenure.objects.create(student=self.student, position='未来职务', starts_on=date(2998, 1, 1))
        CadreTenure.objects.create(student=self.other, position='他人私有职务', starts_on=self.month)
        self.client.force_login(self.student)
        response = self.client.get(reverse('dashboard'))
        for text in ['我的校园名片', '一班', '第一党支部', '班长', '团支书']:
            self.assertContains(response, text)
        for text in ['历史委员', '未来职务', '他人私有职务']:
            self.assertNotContains(response, text)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SuperAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.root = User.objects.create_superuser(username='root-only', full_name='超级管理员', password='RootOnly!2026')
        cls.manager = User.objects.create_user(username='normal-manager', full_name='普通管理员', is_staff=True, password='Manager!2026')
        cls.student = User.objects.create_user(username='normal-student')

    def setUp(self):self.client.force_login(self.root)

    def test_only_root_can_create_manage_admins_and_has_all_business_permissions(self):
        for name in ['manager_list', 'notification_list', 'guide_list', 'leave_list', 'faq_list', 'video_list', 'attendance_list', 'account_list', 'audit_list']:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        self.assertContains(self.client.get(reverse('dashboard')), '管理员管理')
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('manager_list')).status_code, 403)
        self.assertEqual(self.client.post(reverse('manager_create'), {}).status_code, 403)
        self.assertNotContains(self.client.get(reverse('dashboard')), '管理员管理')
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse('manager_list')).status_code, 403)

    def test_root_creation_ignores_injected_roles_and_records_audit(self):
        response = self.client.post(reverse('manager_create'), {'username': 'created-manager', 'full_name': '新老师',
            'department': '学院', 'email': 'manager@example.invalid', 'password1': 'NewManager!2026', 'password2': 'NewManager!2026',
            'is_superuser': '1', 'is_staff': '0', 'is_active': '0', 'is_archived': '1'})
        self.assertEqual(response.status_code, 302)
        item = User.objects.get(username='created-manager')
        self.assertTrue(item.is_staff and item.is_active)
        self.assertFalse(item.is_superuser or item.is_archived)
        self.assertTrue(item.check_password('NewManager!2026'))
        self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)
        self.assertTrue(Audit.objects.filter(action='创建普通管理员').exists())

    def test_disabling_or_forcing_logout_revokes_existing_sessions(self):
        session = Client(); session.force_login(self.manager)
        self.client.post(reverse('manager_action', args=[self.manager.pk, 'logout']))
        self.assertEqual(session.get(reverse('dashboard')).status_code, 302)
        session.force_login(self.manager)
        self.client.post(reverse('manager_action', args=[self.manager.pk, 'disable']))
        self.assertEqual(session.get(reverse('dashboard')).status_code, 302)
        self.manager.refresh_from_db(); self.assertFalse(self.manager.is_active)
        self.client.post(reverse('manager_action', args=[self.manager.pk, 'enable']))
        self.manager.refresh_from_db(); self.assertTrue(self.manager.is_active)
        self.assertEqual(session.get(reverse('dashboard')).status_code, 302)

    def test_root_cannot_be_changed_through_normal_manager_endpoints_or_deleted(self):
        for name, args in [('manager_edit', [self.root.pk]), ('manager_reset', [self.root.pk]), ('manager_action', [self.root.pk, 'disable'])]:
            self.assertEqual(self.client.post(reverse(name, args=args), {}).status_code, 404)
        with self.assertRaises(ProtectedError), transaction.atomic():User.objects.filter(pk=self.root.pk).delete()
        with self.assertRaises(ValidationError):
            self.root.is_active = False; self.root.save()
        self.root.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.root.is_superuser = False; self.root.save()
        with self.assertRaises(ValidationError):User.objects.create_superuser(username='second-root', password='Another!2026')
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.bulk_create([User(username='bulk-root', is_superuser=True, is_staff=True)])
        with self.assertRaises(CommandError):call_command('createsuperuser', interactive=False, username='other-root')

    def test_reset_password_profile_updates_csrf_and_role_isolation(self):
        session = Client(); session.force_login(self.manager)
        self.client.post(reverse('manager_edit', args=[self.manager.pk]), {'full_name': '修改后的老师', 'email': '', 'department': '新单位', 'is_superuser': 1})
        self.client.post(reverse('manager_reset', args=[self.manager.pk]), {'new_password1': 'ChangedManager!2026', 'new_password2': 'ChangedManager!2026'})
        self.manager.refresh_from_db()
        self.assertEqual(self.manager.full_name, '修改后的老师')
        self.assertFalse(self.manager.is_superuser)
        self.assertTrue(self.manager.check_password('ChangedManager!2026'))
        self.assertEqual(session.get(reverse('dashboard')).status_code, 302)
        guarded = Client(enforce_csrf_checks=True); guarded.force_login(self.root)
        self.assertEqual(guarded.post(reverse('manager_action', args=[self.manager.pk, 'disable'])).status_code, 403)

    def test_system_account_backend_is_root_only_even_with_granted_permissions(self):
        self.manager.user_permissions.add(Permission.objects.get(codename='change_user'))
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('admin:portal_user_changelist')).status_code, 403)
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(reverse('admin:portal_user_changelist')).status_code, 200)

    def test_manager_creation_audit_failure_rolls_back(self):
        with patch('portal.manager_views.audit', side_effect=IntegrityError('log failure')), self.assertRaises(IntegrityError):
            self.client.post(reverse('manager_create'), {'username': 'rollback-manager', 'full_name': '老师',
                'password1': 'Distinct!8492Secure', 'password2': 'Distinct!8492Secure'})
        self.assertFalse(User.objects.filter(username='rollback-manager').exists())
