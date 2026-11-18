import io
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from .admin import TemporaryPasswordForm
from .forms import StudentCreationForm
from .inbox import deliver
from .manager_views import ManagerCreationForm
from .models import Conversation, InboxNotice, Leave, Message, Registration, RegistrationAttachment, RegistrationEvent, User
from .services import transition_leave


TEMP = 'Initial!Maple2026'
PERSONAL = 'Personal!Cedar9842'
HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']


@override_settings(PASSWORD_HASHERS=HASHERS)
class PriorityFeatureTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(username='priority-admin', full_name='管理员甲', password=TEMP, is_staff=True)
        cls.admin2 = User.objects.create_user(username='priority-admin2', full_name='管理员乙', is_staff=True)
        cls.disabled = User.objects.create_user(username='priority-disabled', is_staff=True, is_active=False)
        cls.student = User.objects.create_user(username='priority-001', full_name='张同学', password=TEMP)
        cls.other = User.objects.create_user(username='priority-002', full_name='李同学', password=TEMP)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.media = Path(folder.name)
        setting = override_settings(MEDIA_ROOT=self.media)
        setting.enable(); self.addCleanup(setting.disable)
        self.client.force_login(self.student)

    def application_data(self, **extra):
        return {'starts_at': '2026-11-10T08:00', 'ends_at': '2026-12-10T18:00', 'employer': '研究院',
            'position': '研发实习生', 'address': '浙江省杭州市示例路1号301室', 'contact': '13800000000',
            'emergency_name': '家长', 'emergency_contact': '13900000000', 'rental_reason': 'research',
            'has_dorm': 'yes', 'dorm_number': '3栋201',
            'attachments': SimpleUploadedFile('申请表.pdf', b'%PDF-1.4 test'), **extra}

    def create_application(self, kind='internship'):
        response = self.client.post(reverse(kind+'_create'), self.application_data())
        self.assertEqual(response.status_code, 302)
        return Registration.objects.latest('pk')

    def conversation(self):
        response = self.client.post(reverse('consultation_create'), {'subject': '测试咨询', 'category': 'daily', 'body': '问题背景和具体问题'})
        self.assertEqual(response.status_code, 302)
        return Conversation.objects.latest('pk')

    def notice(self, recipient=None, **extra):
        defaults = dict(recipient=recipient or self.student, actor=self.admin, kind='consultation', object_id=1,
            event_key='notice-'+str(InboxNotice.objects.count()), title='咨询收到回复')
        return InboxNotice.objects.create(**{**defaults, **extra})

    def require_password(self, user=None):
        user = user or self.student
        User.objects.filter(pk=user.pk).update(password_change_required=True)
        self.client.force_login(user)

    def test_new_consultation_notifies_only_active_admins(self):
        item = self.conversation()
        notices = InboxNotice.objects.filter(kind='consultation', object_id=item.pk)
        self.assertEqual(set(notices.values_list('recipient_id', flat=True)), {self.admin.pk, self.admin2.pk})
        self.assertFalse(notices.filter(recipient=self.other).exists())

    def test_reply_and_followup_deliver_to_the_other_side(self):
        item = self.conversation()
        self.client.force_login(self.admin)
        self.client.post(reverse('consultation_detail', args=[item.pk]), {'body': '管理员回复'})
        self.assertEqual(InboxNotice.objects.filter(recipient=self.student, object_id=item.pk, kind='consultation').count(), 1)
        self.assertFalse(InboxNotice.objects.filter(recipient=self.other).exists())
        self.client.force_login(self.student)
        self.client.post(reverse('consultation_detail', args=[item.pk]), {'body': '继续追问'})
        item.refresh_from_db(); self.assertTrue(item.awaiting_reply)
        self.assertEqual(InboxNotice.objects.filter(recipient=self.admin, kind='consultation').count(), 2)
        self.assertEqual(InboxNotice.objects.filter(recipient=self.admin2, kind='consultation').count(), 2)

    def test_inbox_counts_filters_pagination_and_escaping_are_personal(self):
        for index in range(15):self.notice(title='<script>bad()</script>' if index == 0 else '回复'+str(index))
        self.notice(self.other, title='其他同学的私密消息')
        response = self.client.get(reverse('inbox_list')+'?status=all&per_page=10&page=2')
        self.assertEqual(response.context['page_obj'].number, 2)
        self.assertEqual(response.context['page_obj'].paginator.count, 15)
        self.assertContains(response, '&lt;script&gt;bad()&lt;/script&gt;')
        self.assertNotContains(response, '其他同学的私密消息')
        self.assertEqual(self.client.get(reverse('inbox_count')).json()['unread'], 15)
        self.assertIn('no-store', response['Cache-Control'])
        for query in ['status=bad', 'kind=bad']:
            self.assertEqual(self.client.get(reverse('inbox_list')+'?'+query).status_code, 400)

    def test_open_marks_only_own_older_topic_notices_and_preserves_new_arrivals(self):
        item = self.conversation()
        first = self.notice(object_id=item.pk)
        second = self.notice(object_id=item.pk)
        later = self.notice(object_id=item.pk)
        unrelated = self.notice(kind='leave')
        other = self.notice(self.other, object_id=item.pk)
        self.assertRedirects(self.client.post(reverse('inbox_open', args=[second.pk])), reverse('consultation_detail', args=[item.pk]))
        for notice in [first, second]:notice.refresh_from_db(); self.assertIsNotNone(notice.read_at)
        for notice in [later, unrelated, other]:notice.refresh_from_db(); self.assertIsNone(notice.read_at)
        self.assertEqual(self.client.post(reverse('inbox_open', args=[other.pk])).status_code, 404)

    def test_admin_read_states_are_independent(self):
        item = self.conversation()
        notice = InboxNotice.objects.get(recipient=self.admin, object_id=item.pk)
        self.client.force_login(self.admin)
        self.client.post(reverse('inbox_open', args=[notice.pk]))
        self.assertFalse(InboxNotice.objects.filter(recipient=self.admin, read_at__isnull=True).exists())
        self.assertTrue(InboxNotice.objects.filter(recipient=self.admin2, read_at__isnull=True).exists())

    def test_read_all_uses_rendered_cutoff_and_never_touches_other_users(self):
        old = self.notice(); other = self.notice(self.other)
        cutoff = self.client.get(reverse('inbox_list')).context['cutoff']
        new = self.notice()
        self.client.post(reverse('inbox_read_all'), {'cutoff': cutoff})
        old.refresh_from_db(); self.assertIsNotNone(old.read_at)
        for notice in [other, new]:notice.refresh_from_db(); self.assertIsNone(notice.read_at)
        self.assertEqual(self.client.post(reverse('inbox_read_all'), {'cutoff': 'bad'}).status_code, 400)

    def test_read_operations_require_post_and_csrf(self):
        notice = self.notice()
        for name, args in [('inbox_open', [notice.pk]), ('inbox_read_all', [])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 405)
            client = Client(enforce_csrf_checks=True); client.force_login(self.student)
            self.assertEqual(client.post(reverse(name, args=args), {'cutoff': notice.pk}).status_code, 403)
        notice.refresh_from_db(); self.assertIsNone(notice.read_at)

    def test_direct_detail_exposes_read_cutoff_without_mutating_on_get(self):
        item = self.conversation()
        notice = self.notice(object_id=item.pk)
        response = self.client.get(reverse('consultation_detail', args=[item.pk]))
        self.assertEqual(response.context['inbox_topic']['cutoff'], notice.pk)
        self.assertContains(response, 'data-inbox-topic-read')
        notice.refresh_from_db(); self.assertIsNone(notice.read_at)
        later = self.notice(object_id=item.pk)
        result = self.client.post(reverse('inbox_read_topic'), {'kind': 'consultation', 'object_id': item.pk, 'cutoff': notice.pk})
        self.assertEqual(result.json()['unread'], 1)
        notice.refresh_from_db(); self.assertIsNotNone(notice.read_at)
        later.refresh_from_db(); self.assertIsNone(later.read_at)

    def test_topic_read_is_personal_validated_post_and_csrf_protected(self):
        own = self.notice(); other = self.notice(self.other)
        data = {'kind': 'consultation', 'object_id': own.object_id, 'cutoff': other.pk}
        self.assertEqual(self.client.get(reverse('inbox_read_topic')).status_code, 405)
        client = Client(enforce_csrf_checks=True); client.force_login(self.student)
        self.assertEqual(client.post(reverse('inbox_read_topic'), data).status_code, 403)
        for extra in [{'kind': 'bad'}, {'object_id': '-1'}, {'cutoff': 'x'}, {'object_id': str(2**64)}]:
            self.assertEqual(self.client.post(reverse('inbox_read_topic'), {**data, **extra}).status_code, 400)
        self.client.post(reverse('inbox_read_topic'), data)
        own.refresh_from_db(); self.assertIsNotNone(own.read_at)
        other.refresh_from_db(); self.assertIsNone(other.read_at)

    def test_notifications_are_idempotent_and_roll_back_with_business_failure(self):
        item = self.conversation()
        message = item.messages.get()
        deliver('consultation', item, self.student, f'message:{message.pk}', '重复通知', to_admins=True)
        self.assertEqual(InboxNotice.objects.count(), 2)
        count = Conversation.objects.count()
        with patch('portal.inbox.InboxNotice.objects.bulk_create', side_effect=RuntimeError('delivery failed')):
            with self.assertRaises(RuntimeError):self.conversation()
        self.assertEqual(Conversation.objects.count(), count)
        self.assertEqual(Message.objects.count(), 1)

    def test_leave_review_and_withdrawal_send_messages_only_when_state_changes(self):
        item = Leave.objects.create(student=self.student, kind='personal', reason='测试', destination='浙江杭州', contact='13800000000',
            starts_at=timezone.now(), ends_at=timezone.now()+timedelta(days=1))
        transition_leave(item.pk, self.admin, 'approve')
        self.assertEqual(InboxNotice.objects.filter(recipient=self.student, kind='leave').count(), 1)
        with self.assertRaises(ValidationError):transition_leave(item.pk, self.admin, 'approve')
        self.assertEqual(InboxNotice.objects.filter(recipient=self.student, kind='leave').count(), 1)

    def test_new_student_and_manager_forms_require_initial_password_change(self):
        form = StudentCreationForm(data={'username': 'new-student', 'full_name': '新同学', 'id_number': '110000199001010001',
            'enrollment_status': 'enrolled', 'student_class': '新生班', 'password1': TEMP, 'password2': TEMP})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.save().password_change_required)
        manager = ManagerCreationForm(data={'username': 'new-manager', 'full_name': '新管理员', 'password1': TEMP, 'password2': TEMP})
        self.assertTrue(manager.is_valid(), manager.errors)
        self.assertTrue(manager.save().password_change_required)

    def test_required_change_blocks_all_business_pages_and_post_writes(self):
        self.require_password()
        for name in ['dashboard', 'notification_list', 'attendance_list', 'inbox_list', 'password_change_done']:
            self.assertRedirects(self.client.get(reverse(name)), reverse('password_change'))
        response = self.client.post(reverse('consultation_create'), {'subject': '绕过改密', 'category': 'daily', 'body': '不应提交'})
        self.assertRedirects(response, reverse('password_change'))
        self.assertFalse(Conversation.objects.exists())
        response = self.client.get(reverse('inbox_count'), HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['redirect'], reverse('password_change'))
        self.assertContains(self.client.get(reverse('password_change')), '请先设置自己的新密码')
        self.assertEqual(self.client.get(reverse('health')).status_code, 200)

    def test_forced_change_rejects_same_weak_wrong_and_mismatched_passwords(self):
        self.require_password()
        for old, first, second in [(TEMP, TEMP, TEMP), (TEMP, '123', '123'), ('wrong', PERSONAL, PERSONAL), (TEMP, PERSONAL, 'mismatch')]:
            response = self.client.post(reverse('password_change'), {'old_password': old, 'new_password1': first, 'new_password2': second})
            self.assertEqual(response.status_code, 200)
            self.student.refresh_from_db(); self.assertTrue(self.student.password_change_required)
            self.assertTrue(self.student.check_password(TEMP))

    def test_successful_change_keeps_this_session_and_revokes_other_sessions(self):
        self.require_password()
        other_session = Client(); other_session.force_login(User.objects.get(pk=self.student.pk))
        self.assertRedirects(self.client.post(reverse('password_change'), {'old_password': TEMP, 'new_password1': PERSONAL,
            'new_password2': PERSONAL}), reverse('password_change_done'))
        self.student.refresh_from_db(); self.assertFalse(self.student.password_change_required)
        self.assertTrue(self.student.check_password(PERSONAL))
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)
        self.assertEqual(other_session.get(reverse('dashboard')).status_code, 302)
        self.assertFalse(Client().login(username=self.student.username, password=TEMP))
        self.assertTrue(Client().login(username=self.student.username, password=PERSONAL))

    def test_password_change_requires_old_password_csrf_and_rolls_back_on_audit_failure(self):
        self.require_password()
        client = Client(enforce_csrf_checks=True); client.force_login(self.student)
        self.assertEqual(client.post(reverse('password_change'), {}).status_code, 403)
        with patch('portal.account_security.audit', side_effect=RuntimeError('audit failed')):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse('password_change'), {'old_password': TEMP, 'new_password1': PERSONAL, 'new_password2': PERSONAL})
        self.student.refresh_from_db(); self.assertTrue(self.student.password_change_required)
        self.assertTrue(self.student.check_password(TEMP))

    def test_student_reset_requires_new_change_and_invalidates_even_same_temp_password(self):
        old_session = Client(); old_session.force_login(self.student)
        self.client.force_login(self.admin)
        self.client.post(reverse('account_reset', args=[self.student.pk]), {'new_password1': TEMP, 'new_password2': TEMP})
        self.student.refresh_from_db(); self.assertTrue(self.student.password_change_required)
        self.assertEqual(old_session.get(reverse('dashboard')).status_code, 302)
        client = Client(); self.assertTrue(client.login(username=self.student.username, password=TEMP))
        self.assertRedirects(client.get(reverse('dashboard')), reverse('password_change'))

    def test_native_admin_password_reset_also_requires_change(self):
        form = TemporaryPasswordForm(self.student, data={'password1': PERSONAL, 'password2': PERSONAL, 'usable_password': 'true'})
        self.assertTrue(form.is_valid(), form.errors)
        form.save(); self.student.refresh_from_db()
        self.assertTrue(self.student.password_change_required)
        self.assertTrue(self.student.check_password(PERSONAL))

    def test_command_line_reset_also_requires_change(self):
        from django.core.management import call_command
        with patch('django.contrib.auth.management.commands.changepassword.Command._get_pass', return_value=PERSONAL):
            call_command('changepassword', self.student.username, stdout=io.StringIO())
        self.student.refresh_from_db(); self.assertTrue(self.student.password_change_required)
        self.assertTrue(self.student.check_password(PERSONAL))

    def test_both_kinds_withdraw_and_resubmit_preserve_original_and_attachments(self):
        for kind in ['internship', 'rental']:
            original = self.create_application(kind)
            old_file = original.attachments.get()
            response = self.client.post(reverse(kind+'_withdraw', args=[original.pk]), {'confirmed': 'yes'})
            self.assertRedirects(response, reverse(kind+'_resubmit', args=[original.pk]))
            original.refresh_from_db(); self.assertEqual(original.status, 'withdrawn')
            form_page = self.client.get(reverse(kind+'_resubmit', args=[original.pk]))
            self.assertEqual(form_page.context['form']['address'].value(), original.address)
            if kind == 'rental':self.assertEqual(form_page.context['form']['has_dorm'].value(), 'yes')
            data = self.application_data(address='浙江省杭州市新地址9号302室', retain_attachments=[str(old_file.pk)])
            data.pop('attachments')
            response = self.client.post(reverse(kind+'_resubmit', args=[original.pk]), data)
            self.assertEqual(response.status_code, 302)
            replacement = Registration.objects.get(previous=original)
            self.assertEqual(replacement.status, 'pending')
            self.assertEqual(replacement.address, data['address'])
            self.assertEqual(replacement.attachments.get().file.name, old_file.file.name)
            original.refresh_from_db(); self.assertEqual(original.status, 'withdrawn')
            self.assertNotEqual(original.address, replacement.address)
            self.assertTrue(old_file.file.storage.exists(old_file.file.name))
            self.assertEqual(original.events.count(), 2)
            self.assertContains(self.client.get(reverse(kind+'_detail', args=[original.pk])), '新申请 #')
            self.assertContains(self.client.get(reverse(kind+'_detail', args=[replacement.pk])), '原申请 #')

    def test_rejected_application_copies_original_and_retains_rejection_history(self):
        original = self.create_application()
        self.client.force_login(self.admin)
        self.client.post(reverse('internship_review', args=[original.pk]), {'action': 'reject', 'note': '请补充实习岗位证明'})
        self.assertEqual(InboxNotice.objects.filter(recipient=self.student, kind='internship').count(), 1)
        self.client.force_login(self.student)
        self.assertContains(self.client.get(reverse('internship_detail', args=[original.pk])), '修改并重新提交')
        response = self.client.post(reverse('internship_resubmit', args=[original.pk]), self.application_data())
        self.assertEqual(response.status_code, 302)
        original.refresh_from_db(); self.assertEqual(original.status, 'rejected')
        self.assertTrue(original.events.filter(note='请补充实习岗位证明').exists())
        new = Registration.objects.get(previous=original)
        self.assertNotEqual(new.attachments.get().file.name, original.attachments.get().file.name)
        self.assertEqual(new.student_id, original.student_id)

    def test_withdraw_requires_owner_post_confirmation_and_csrf(self):
        item = self.create_application()
        url = reverse('internship_withdraw', args=[item.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.post(url, {})
        item.refresh_from_db(); self.assertEqual(item.status, 'pending')
        client = Client(enforce_csrf_checks=True); client.force_login(self.student)
        self.assertEqual(client.post(url, {'confirmed': 'yes'}).status_code, 403)
        for user, status in [(self.other, 404), (self.admin, 403)]:
            self.client.force_login(user)
            self.assertEqual(self.client.post(url, {'confirmed': 'yes'}).status_code, status)
            self.assertEqual(self.client.get(reverse('internship_resubmit', args=[item.pk])).status_code, status)

    def test_approved_applications_cannot_be_withdrawn_or_resubmitted(self):
        item = self.create_application()
        Registration.objects.filter(pk=item.pk).update(status='approved')
        before = Registration.objects.values().get(pk=item.pk)
        self.client.post(reverse('internship_withdraw', args=[item.pk]), {'confirmed': 'yes'})
        self.client.post(reverse('internship_resubmit', args=[item.pk]), self.application_data())
        self.assertEqual(Registration.objects.values().get(pk=item.pk), before)
        self.assertEqual(Registration.objects.count(), 1)
        self.assertEqual(item.events.count(), 1)

    def test_withdrawn_application_cannot_be_reviewed_and_no_duplicate_messages(self):
        item = self.create_application()
        self.client.post(reverse('internship_withdraw', args=[item.pk]), {'confirmed': 'yes'})
        count = InboxNotice.objects.count()
        self.client.post(reverse('internship_withdraw', args=[item.pk]), {'confirmed': 'yes'})
        self.client.force_login(self.admin)
        self.client.post(reverse('internship_review', args=[item.pk]), {'action': 'approve'})
        item.refresh_from_db(); self.assertEqual(item.status, 'withdrawn')
        self.assertEqual(item.events.count(), 2)
        self.assertEqual(InboxNotice.objects.count(), count)

    def test_resubmit_rejects_foreign_attachment_empty_attachments_and_wrong_kind(self):
        item = self.create_application()
        foreign = self.create_application('rental').attachments.get()
        self.client.post(reverse('internship_withdraw', args=[item.pk]), {'confirmed': 'yes'})
        for selected in [[], [str(foreign.pk)]]:
            data = self.application_data(retain_attachments=selected); data.pop('attachments')
            response = self.client.post(reverse('internship_resubmit', args=[item.pk]), data)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['form'].errors)
        self.assertFalse(Registration.objects.filter(previous=item).exists())
        self.assertEqual(self.client.get(reverse('rental_resubmit', args=[item.pk])).status_code, 404)

    def test_combined_retained_and_new_attachment_limit_and_missing_file(self):
        item = self.create_application()
        file = item.attachments.get()
        self.client.post(reverse('internship_withdraw', args=[item.pk]), {'confirmed': 'yes'})
        data = self.application_data(retain_attachments=[str(file.pk)],
            attachments=[SimpleUploadedFile(str(index)+'.pdf', b'%PDF-1.4 test') for index in range(10)])
        response = self.client.post(reverse('internship_resubmit', args=[item.pk]), data)
        self.assertIn('attachments', response.context['form'].errors)
        file.file.storage.delete(file.file.name)
        data = self.application_data(retain_attachments=[str(file.pk)]); data.pop('attachments')
        response = self.client.post(reverse('internship_resubmit', args=[item.pk]), data)
        self.assertContains(response, '原附件已不可用')
        self.assertFalse(Registration.objects.filter(previous=item).exists())

    def test_repeat_resubmit_returns_existing_child_instead_of_duplicating(self):
        item = self.create_application()
        Registration.objects.filter(pk=item.pk).update(status='rejected')
        url = reverse('internship_resubmit', args=[item.pk])
        self.client.post(url, self.application_data())
        new = Registration.objects.get(previous=item)
        self.assertRedirects(self.client.post(url, self.application_data()), reverse('internship_detail', args=[new.pk]))
        self.assertEqual(Registration.objects.filter(previous=item).count(), 1)
        self.assertEqual(item.attachments.count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Registration.objects.create(previous=item, student=self.student, kind='internship', starts_at=item.starts_at, ends_at=item.ends_at)

    def test_resubmit_race_rechecks_original_before_writing(self):
        item = self.create_application()
        Registration.objects.filter(pk=item.pk).update(status='rejected')
        from .registration_services import save_registration
        def changed(form, actor, kind, source=None):
            Registration.objects.filter(pk=source.pk).update(status='approved')
            return save_registration(form, actor, kind, source)
        with patch('portal.registration_views.save_registration', side_effect=changed):
            response = self.client.post(reverse('internship_resubmit', args=[item.pk]), self.application_data())
        self.assertContains(response, '原申请状态已变化')
        self.assertFalse(Registration.objects.filter(previous=item).exists())
        self.assertEqual(len(list(self.media.rglob('*.pdf'))), 1)

    def test_resubmit_failure_removes_only_new_uploads_and_keeps_original_files(self):
        item = self.create_application()
        Registration.objects.filter(pk=item.pk).update(status='rejected')
        original_file = item.attachments.get().file
        with patch('portal.registration_services.audit', side_effect=RuntimeError('audit failed')):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse('internship_resubmit', args=[item.pk]), self.application_data())
        self.assertFalse(Registration.objects.filter(previous=item).exists())
        self.assertTrue(original_file.storage.exists(original_file.name))
        self.assertEqual(len(list(self.media.rglob('*.pdf'))), 1)


class PriorityMigrationTests(TransactionTestCase):
    def test_migration_flags_only_never_logged_in_preserves_all_old_data_and_does_not_backfill_messages(self):
        before = [('portal', '0014_notification_click_metrics')]
        after = [('portal', '0015_inbox_password_registration_corrections')]
        def restore():
            executor = MigrationExecutor(connection); executor.migrate(executor.loader.graph.leaf_nodes())
        self.addCleanup(restore)
        executor = MigrationExecutor(connection); executor.migrate(before)
        old = executor.loader.project_state(before).apps
        users = old.get_model('portal', 'User')
        initial = users.objects.create(username='never-logged-in', password='saved-initial-hash')
        used = users.objects.create(username='used-account', password='saved-personal-hash', last_login=timezone.now())
        unusable = users.objects.create(username='unusable-account', password='!unusable')
        admin = users.objects.create(username='history-admin', is_staff=True, password='initial-admin-hash')
        regs = old.get_model('portal', 'Registration')
        original = regs.objects.create(student=used, kind='rental', status='rejected', starts_at=timezone.now(), ends_at=timezone.now()+timedelta(days=1), address='原地址')
        originals = list(users.objects.values())
        registration = regs.objects.values().get(pk=original.pk)
        executor = MigrationExecutor(connection); executor.migrate(after)
        current = executor.loader.project_state(after).apps
        updated = current.get_model('portal', 'User')
        for row in originals:
            self.assertEqual(updated.objects.values(*row.keys()).get(pk=row['id']), row)
        self.assertTrue(updated.objects.get(pk=initial.pk).password_change_required)
        self.assertTrue(updated.objects.get(pk=admin.pk).password_change_required)
        self.assertFalse(updated.objects.get(pk=used.pk).password_change_required)
        self.assertFalse(updated.objects.get(pk=unusable.pk).password_change_required)
        self.assertEqual(current.get_model('portal', 'Registration').objects.values(*registration.keys()).get(pk=original.pk), registration)
        self.assertFalse(current.get_model('portal', 'InboxNotice').objects.exists())
