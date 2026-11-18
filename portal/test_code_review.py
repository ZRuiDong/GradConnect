import io
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook, load_workbook
from gradconnect.database import postgres_config
from .models import AttendanceBatch, CadreTenure, MonthlyAttendance, StudentImportBatch, StudentRosterBatch, User
from .roster_imports import HEADERS, MAX_ROWS, confirm_roster, parse_roster
from .services import confirm_attendance, confirm_student_accounts


def streaming_roster(kind, rows):
    book = Workbook(write_only=True)
    sheet = book.create_sheet('名单')
    sheet.append(HEADERS[kind])
    for row in rows:
        sheet.append(row)
    output = io.BytesIO(); book.save(output)
    return SimpleUploadedFile('roster.xlsx', output.getvalue())


def change_sheet(upload, change):
    output = io.BytesIO()
    with ZipFile(upload) as source, ZipFile(output, 'w') as target:
        for member in source.infolist():
            content = source.read(member.filename)
            if member.filename == 'xl/worksheets/sheet1.xml':
                root = ET.fromstring(content); change(root); content = ET.tostring(root)
            target.writestr(member, content)
    return SimpleUploadedFile('roster.xlsx', output.getvalue())


NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ReviewRegressionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(username='review-admin', is_staff=True)
        cls.student = User.objects.create_user(username='000-review-1', full_name='检查学生', student_class='测试班')

    def test_streaming_excel_without_dimensions_is_a_valid_roster(self):
        upload = streaming_roster('graduate', [[self.student.username, self.student.full_name]])
        result = parse_roster(upload, 'graduate')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['student_id'], self.student.pk)

    def test_ragged_optional_end_date_still_defaults_to_2999(self):
        upload = streaming_roster('tenure', [[self.student.username, self.student.full_name, '委员', '2026-01-01']])
        result = parse_roster(upload, 'tenure')
        self.assertEqual(result[0]['ends_on'], '2999-12-31')

    def test_underreported_dimension_does_not_silently_drop_rows(self):
        other = User.objects.create_user(username='000-review-2', full_name='第二位学生')
        upload = streaming_roster('graduate', [[self.student.username, self.student.full_name], [other.username, other.full_name]])
        def small_range(root):root.insert(0, ET.Element(NS+'dimension', {'ref': 'A1:B1'}))
        rows = parse_roster(change_sheet(upload, small_range), 'graduate')
        self.assertEqual({row['student_id'] for row in rows}, {self.student.pk, other.pk})

    def test_actual_oversized_rows_and_columns_rejected_even_without_dimensions(self):
        def far_row(root):
            row = root.find(NS+'sheetData').findall(NS+'row')[1]
            row.set('r', str(MAX_ROWS+2))
            for cell in row:cell.set('r', cell.get('r')[0]+str(MAX_ROWS+2))
        def wide_column(root):
            row = root.find(NS+'sheetData').findall(NS+'row')[1]
            row[-1].set('r', 'XFD2')
        for change in [far_row, wide_column]:
            upload = streaming_roster('graduate', [[self.student.username, self.student.full_name]])
            with self.subTest(kind=change.__name__), self.assertRaises(ValidationError):
                parse_roster(change_sheet(upload, change), 'graduate')

    def test_unrelated_student_history_does_not_block_target_import(self):
        unrelated = User.objects.create_user(username='unrelated', full_name='其他学生')
        CadreTenure.objects.bulk_create([CadreTenure(student=unrelated, position=f'旧职务{index}', starts_on=date(2020, 1, 1)) for index in range(101)])
        upload = streaming_roster('tenure', [[self.student.username, self.student.full_name, '班长', '2026-01-01']])
        rows = parse_roster(upload, 'tenure')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['student_id'], self.student.pk)

    def test_deleted_previews_are_validation_errors_for_all_import_services(self):
        for model, confirm in [(AttendanceBatch, confirm_attendance), (StudentImportBatch, confirm_student_accounts), (StudentRosterBatch, confirm_roster)]:
            batch = model.objects.create(uploaded_by=self.admin, row_count=1)
            pk = batch.pk; batch.delete()
            with self.subTest(model=model.__name__), self.assertRaisesMessage(ValidationError, '预览已取消或被清理'):
                confirm(pk, self.admin)

    def test_confirmation_cancel_race_returns_to_list_without_500_or_writes(self):
        client = Client(); client.force_login(self.admin)
        cases = [(AttendanceBatch, confirm_attendance, 'portal.views.confirm_attendance', 'attendance_preview', 'attendance_list'),
                 (StudentImportBatch, confirm_student_accounts, 'portal.views.confirm_student_accounts', 'account_preview', 'account_list'),
                 (StudentRosterBatch, confirm_roster, 'portal.roster_views.confirm_roster', 'roster_preview', 'account_list')]
        for model, confirm, target, preview, listing in cases:
            batch = model.objects.create(uploaded_by=self.admin, row_count=1, payload=[{'month': '2026-11'}])
            def cancel_before_confirm(pk, actor):
                model.objects.filter(pk=pk).delete()
                return confirm(pk, actor)
            with self.subTest(preview=preview), patch(target, side_effect=cancel_before_confirm):
                response = client.post(reverse(preview, args=[batch.pk]), {'action': 'confirm'}, follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.redirect_chain[-1][0], reverse(listing))
                self.assertContains(response, '预览已取消或被清理')
        self.assertEqual(MonthlyAttendance.objects.count(), 0)

    def test_attendance_report_fetches_roles_once_across_multiple_chunks(self):
        batch = AttendanceBatch.objects.create(uploaded_by=self.admin, row_count=600, is_confirmed=True)
        CadreTenure.objects.create(student=self.student, position='班长', starts_on=date(2020, 1, 1))
        MonthlyAttendance.objects.bulk_create([MonthlyAttendance(student=self.student, student_name=self.student.full_name,
            student_number=self.student.username, month=date(1900+index//12, index%12+1, 1), clock_days=20, leave_days=2, batch=batch) for index in range(600)])
        client = Client(); client.force_login(self.admin)
        with CaptureQueriesContext(connection) as queries:
            response = client.get(reverse('attendance_export'), {'month_from': '', 'month_to': ''})
            raw = b''.join(response.streaming_content); response.close()
        role_queries = [query['sql'] for query in queries if 'FROM "portal_cadretenure"' in query['sql']]
        self.assertEqual(len(role_queries), 1)
        book = load_workbook(io.BytesIO(raw), read_only=True)
        rows = list(book['月度打卡明细'].iter_rows(min_row=6, values_only=True))
        self.assertEqual(len(rows), 600)
        self.assertTrue(all(row[13]=='班长' for row in rows))
        book.close()


class DatabaseDeploymentConfigTests(SimpleTestCase):
    def test_postgres_url_decodes_identifiers_and_has_health_check_timeout(self):
        config = postgres_config('postgresql://school%40app:p%40ss%26word@127.0.0.1:5433/grad%20connect')
        self.assertEqual((config['USER'], config['PASSWORD'], config['NAME'], config['PORT']), ('school@app', 'p@ss&word', 'grad connect', 5433))
        self.assertTrue(config['CONN_HEALTH_CHECKS'])
        self.assertEqual(config['OPTIONS']['connect_timeout'], 5)

    def test_cloud_database_tls_parameters_are_not_silently_ignored(self):
        config = postgres_config('postgresql://user:pass@db.example.invalid/app?sslmode=verify-full&sslrootcert=%2Fcerts%2Froot.crt&connect_timeout=10&application_name=grad-web')
        self.assertEqual(config['OPTIONS'], {'sslmode':'verify-full', 'sslrootcert':'/certs/root.crt', 'connect_timeout':10, 'application_name':'grad-web'})

    def test_unix_socket_database_uses_empty_host_not_none(self):
        self.assertEqual(postgres_config('postgresql:///gradconnect')['HOST'], '')

    def test_invalid_unknown_duplicate_or_empty_options_fail_without_secret_leak(self):
        urls = ['mysql://user:do-not-leak@host/app', 'postgresql://user:do-not-leak@host',
                'postgresql://user:do-not-leak@host/app?sslmode=invalid', 'postgresql:///app?connect_timeout=0',
                'postgresql:///app?connect_timeout=121', 'postgresql:///app?connect_timeout=bad',
                'postgresql:///app?sslmode=require&sslmode=disable', 'postgresql:///app?unknown=secret',
                'postgresql:///app?sslmode=', 'postgresql:///app#fragment', 'postgresql://host:invalid/app']
        for url in urls:
            with self.subTest(url=url.split('?')[0].split('@')[-1]):
                with self.assertRaises(ImproperlyConfigured) as error:postgres_config(url)
                self.assertNotIn('do-not-leak', str(error.exception))

    def test_backup_uses_same_tls_and_timeout_options_without_password_in_arguments(self):
        from portal.management.commands.backup_data import Command
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = postgres_config('postgresql://user:private-pass@db.example.invalid/app?sslmode=verify-full&sslrootcert=%2Fcerts%2Froot.crt&connect_timeout=9')
            settings = SimpleNamespace(BASE_DIR=root, MEDIA_ROOT=root/'media', DATABASES={'default':config})
            def dump(args, **kwargs):
                Path(args[-1]).write_bytes(b'test-pg-dump')
                self.assertNotIn('private-pass', ' '.join(map(str, args)))
                self.assertEqual(kwargs['env']['PGSSLMODE'], 'verify-full')
                self.assertEqual(kwargs['env']['PGSSLROOTCERT'], '/certs/root.crt')
                self.assertEqual(kwargs['env']['PGCONNECT_TIMEOUT'], '9')
                self.assertEqual(kwargs['env']['PGPASSWORD'], 'private-pass')
                return SimpleNamespace(returncode=0)
            with patch('portal.management.commands.backup_data.settings', settings), patch('portal.management.commands.backup_data.shutil.which', return_value='/test/pg_dump'), patch('portal.management.commands.backup_data.subprocess.run', side_effect=dump):
                Command(stdout=io.StringIO()).handle(destination=str(root/'backups'))
            self.assertEqual(len(list((root/'backups').glob('*.tar.gz'))), 1)
