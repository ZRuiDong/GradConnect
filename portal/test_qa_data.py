import calendar
import io
import tempfile
from datetime import date,timedelta
from pathlib import Path
from unittest.mock import patch
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from .models import (Audit, CadreTenure, Conversation, FAQ, InquiryCategory, Leave, Message, MonthlyAttendance,
    Notification, Policy, PolicyRevision, Publication, Registration, ServiceGuide, StudentRosterBatch, User, Video)
from .qa_data import GROUP, LEGACY_TEST_NAMES, MEDIA_PREFIX, PASSWORD, PREFIX, clear_dataset, dataset_report, repair_test_names, seed_dataset
from .services import parse_attendance, parse_student_accounts
from .student_services import protected_student_ids, student_queryset
from .uploads import validate_notification_upload, validate_registration_upload
from django.core.files.uploadedfile import SimpleUploadedFile


@override_settings(DEBUG=True,PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ComprehensiveDataTests(TestCase):
    def setUp(self):
        self.media=tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        change=override_settings(MEDIA_ROOT=Path(self.media.name))
        change.enable();self.addCleanup(change.disable)
        self.original=User.objects.create_user(username="original",password="Keep!password2026",full_name="原学生")
        self.admin=User.objects.create_user(username="original-admin",is_staff=True)
        self.notice=Notification.objects.create(title=PREFIX+"原有同名前缀通知",body="原内容",created_by=self.admin)

    def seed(self):
        return seed_dataset(60,date(2026,11,17))

    def test_seed_covers_modules_categories_statuses_calendar_and_special_accounts(self):
        report,created=self.seed()
        self.assertTrue(created)
        self.assertEqual(report["counts"]["students"],60)
        self.assertEqual(report["counts"]["admins"],3)
        self.assertEqual(report["counts"]["notifications"],18 * len(Notification.Category))
        self.assertEqual(report["counts"]["guides"],128)
        self.assertEqual(report["counts"]["faqs"],192)
        self.assertEqual(set(Notification.objects.filter(created_by__username="qa_admin").values_list("category",flat=True)),set(Notification.Category.values))
        self.assertEqual(set(PolicyRevision.objects.filter(policy__created_by__username="qa_admin").values_list("category",flat=True)),set(PolicyRevision.Category.values))
        self.assertEqual(set(FAQ.objects.filter(created_by__username="qa_admin").values_list("category",flat=True)),set(InquiryCategory.values))
        self.assertEqual(set(Leave.objects.filter(student__username="qa000001").values_list("status",flat=True)),set(Leave.Status.values))
        for kind in Registration.Kind.values:
            self.assertEqual(set(Registration.objects.filter(student__username="qa000001",kind=kind).values_list("status",flat=True)),set(Registration.Status.values))
        for item in MonthlyAttendance.objects.all():
            self.assertLessEqual(item.total_days,calendar.monthrange(item.month.year,item.month.month)[1])
        self.assertTrue(MonthlyAttendance.objects.filter(month="2024-02-01",clock_days=29).exists())
        self.assertTrue(MonthlyAttendance.objects.filter(clock_days=0,leave_days=0).exists())
        self.assertEqual(User.objects.get(username="qa000003").enrollment_status,"graduated")
        self.assertFalse(User.objects.get(username="qa000004").is_active)
        self.assertTrue(User.objects.get(username="qa000005").is_archived)
        self.assertFalse(User.objects.get(username="qa000006").profile_complete)
        self.assertEqual(CadreTenure.objects.filter(student__username="qa000013").count(),100)
        self.assertEqual(len(student_queryset().get(username="qa000001").current_tenures),2)
        for model,relation in [(Leave,'events'),(Registration,'events'),(Conversation,'messages')]:
            for item in model.objects.prefetch_related(relation):
                latest=max(event.created_at for event in getattr(item,relation).all())
                self.assertGreaterEqual(item.updated_at,latest-timedelta(seconds=1))
        free=list(User.objects.filter(groups__name=GROUP,username__gte="qa000041",is_staff=False).values_list("pk",flat=True))
        self.assertEqual(protected_student_ids(free),set())

    def test_repeat_preserves_edits_passwords_and_original_records(self):
        original=User.objects.values().get(pk=self.original.pk)
        self.seed()
        user=User.objects.get(username="qa000001")
        user.full_name="测试时修改的姓名";user.set_password("Changed!password2026");user.save()
        count=User.objects.count()
        report,created=self.seed()
        self.assertFalse(created)
        self.assertEqual(User.objects.count(),count)
        user.refresh_from_db()
        self.assertEqual(user.full_name,"测试时修改的姓名")
        self.assertTrue(user.check_password("Changed!password2026"))
        self.assertEqual(User.objects.values().get(pk=self.original.pk),original)
        self.notice.refresh_from_db();self.assertEqual(self.notice.body,"原内容")

    def test_new_dataset_names_are_readable_and_nonempty(self):
        self.seed()
        for index in [15, 16, 18, 19]:
            self.assertEqual(User.objects.get(username=f"qa{index:06}").full_name, f"测试学生{index:04}")
        for user in User.objects.filter(groups__name=GROUP, is_staff=False):
            self.assertRegex(user.full_name, r"^测试学生\d{4}$")

    def test_name_repair_updates_known_snapshots_preserves_edits_and_is_idempotent(self):
        self.seed()
        from django.forms.models import model_to_dict
        before_original=model_to_dict(self.original)
        for index, legacy_name in LEGACY_TEST_NAMES.items():
            user=User.objects.get(username=f"qa{index:06}")
            User.objects.filter(pk=user.pk).update(full_name=legacy_name)
            for model in [Leave, Registration, MonthlyAttendance]:
                model.objects.filter(student=user).update(student_name=user.username if not legacy_name and model is Registration else legacy_name)
        edited=User.objects.get(username="qa000016")
        User.objects.filter(pk=edited.pk).update(full_name="管理员已核对的姓名")
        edited_row=Leave.objects.filter(student=edited).first()
        Leave.objects.filter(pk=edited_row.pk).update(student_name="人工保留的历史姓名")
        protected_user=User.objects.get(username="qa000015")
        original_state=User.objects.values().get(pk=protected_user.pk)
        result=repair_test_names()
        self.assertEqual(result["users"],3)
        for index in [15, 18, 19]:
            user=User.objects.get(username=f"qa{index:06}")
            self.assertEqual(user.full_name,f"测试学生{index:04}")
            for model in [Leave, Registration, MonthlyAttendance]:
                self.assertFalse(model.objects.filter(student=user).exclude(student_name=user.full_name).exists())
        edited.refresh_from_db();edited_row.refresh_from_db()
        self.assertEqual(edited.full_name,"管理员已核对的姓名")
        self.assertEqual(edited_row.student_name,"人工保留的历史姓名")
        self.assertTrue(MonthlyAttendance.objects.filter(student=edited,student_name=edited.full_name).exists())
        current_state=User.objects.values().get(pk=protected_user.pk)
        self.assertEqual({key:value for key,value in current_state.items() if key!='full_name'},
                         {key:value for key,value in original_state.items() if key!='full_name'})
        self.assertEqual(model_to_dict(User.objects.get(pk=self.original.pk)),before_original)
        self.assertEqual(repair_test_names(),dict.fromkeys(result,0))

    def test_name_repair_is_guarded_and_command_backs_up(self):
        self.assertEqual(repair_test_names(),{"users":0,"leaves":0,"registrations":0,"monthly_attendance":0})
        self.seed()
        with override_settings(DEBUG=False),self.assertRaises(CommandError):
            repair_test_names()
        with patch('portal.management.commands.seed_test_data.call_command') as backup:
            call_command('seed_test_data',repair_names=True,stdout=io.StringIO())
        backup.assert_called_once()
        with self.assertRaises(CommandError):
            call_command('seed_test_data',repair_names=True,check=True,stdout=io.StringIO())

    def test_student_permissions_private_conversations_withdrawal_and_video_range(self):
        self.seed()
        first=User.objects.get(username="qa000001");second=User.objects.get(username="qa000002")
        self.client.force_login(first)
        own=Conversation.objects.filter(student=first).first()
        other=Conversation.objects.filter(student=second).first()
        self.assertEqual(self.client.get(reverse("consultation_detail",args=[own.pk])).status_code,200)
        self.assertEqual(self.client.get(reverse("consultation_detail",args=[other.pk])).status_code,404)
        self.assertEqual(self.client.get(reverse("account_list")).status_code,403)
        withdrawn=Notification.objects.filter(status="withdrawn").first()
        self.assertEqual(self.client.get(reverse("notification_detail",args=[withdrawn.pk])).status_code,404)
        video=Video.objects.filter(is_deleted=False).first()
        response=self.client.get(reverse("video_file",args=[video.pk]),HTTP_RANGE="bytes=0-31")
        self.assertEqual(response.status_code,206)
        self.assertEqual(len(b"".join(response.streaming_content)),32);response.close()
        self.assertNotIn("QA-PRIVATE-",''.join(FAQ.objects.values_list("answer",flat=True)))

    def test_seeded_files_and_import_samples_are_valid(self):
        self.seed()
        root=Path(self.media.name)/MEDIA_PREFIX
        from zipfile import ZipFile
        with ZipFile(root/"fixture-bundle.zip") as package:
            rows=parse_attendance(SimpleUploadedFile("monthly.csv",package.read("valid/monthly-attendance.csv")))
            self.assertTrue(rows)
            updates=parse_student_accounts(SimpleUploadedFile("update.csv",package.read("valid/update-politics.csv")),mode="update")
            self.assertTrue(updates)
            creates=parse_student_accounts(SimpleUploadedFile("new.csv",package.read("valid/new-students.csv")))
            self.assertEqual(len(creates),50)
            for name in package.namelist():
                if name.startswith("invalid/") and name.endswith(".csv"):
                    from django.core.exceptions import ValidationError
                    with self.assertRaises(ValidationError):
                        parse_attendance(SimpleUploadedFile(name,package.read(name)))
        for item in Notification.objects.filter(created_by__username="qa_admin").exclude(attachment=""):
            with item.attachment.open("rb") as file:
                validate_notification_upload(SimpleUploadedFile(item.attachment.name,file.read()))
        for item in Registration.objects.prefetch_related("attachments"):
            for attachment in item.attachments.all():
                with attachment.file.open("rb") as file:
                    validate_registration_upload(SimpleUploadedFile(attachment.file.name,file.read()))

    def test_clear_only_removes_tagged_data_and_shared_files_stay_if_referenced(self):
        self.seed()
        kept=Notification.objects.create(title="外部仍引用测试PDF",body="保留文件",created_by=self.admin,attachment=MEDIA_PREFIX+"sample.pdf")
        self.assertTrue(clear_dataset())
        self.assertTrue(User.objects.filter(pk=self.original.pk).exists())
        self.assertTrue(Notification.objects.filter(pk=self.notice.pk).exists())
        self.assertTrue(Notification.objects.filter(pk=kept.pk).exists())
        self.assertTrue((Path(self.media.name)/MEDIA_PREFIX/"sample.pdf").exists())
        self.assertFalse(Group.objects.filter(name=GROUP).exists())
        self.assertFalse(dataset_report()["exists"])

    def test_clear_then_regenerate_and_default_clear_removes_dedicated_files(self):
        self.seed()
        self.assertTrue(clear_dataset())
        self.assertFalse((Path(self.media.name)/MEDIA_PREFIX).exists())
        self.assertEqual(User.objects.count(),2)
        report,created=self.seed()
        self.assertTrue(created);self.assertEqual(report["counts"]["students"],60)

    def test_protected_non_test_association_blocks_clear_without_partial_deletion(self):
        self.seed()
        qa=User.objects.get(username="qa_admin")
        extra=Notification.objects.create(title="应保留的非测试前缀内容",body="保留",created_by=qa)
        count=Notification.objects.count()
        with self.assertRaises(CommandError):
            clear_dataset()
        self.assertEqual(Notification.objects.count(),count)
        self.assertTrue(Notification.objects.filter(pk=extra.pk).exists())
        self.assertTrue(dataset_report()["exists"])

    def test_failure_rolls_back_database_and_only_cleans_new_files(self):
        with patch("portal.qa_data.Policy.objects.create",side_effect=IntegrityError("test rollback")),self.assertRaises(IntegrityError):
            self.seed()
        self.assertEqual(User.objects.count(),2)
        self.assertEqual(Notification.objects.count(),1)
        self.assertFalse(Group.objects.filter(name=GROUP).exists())
        self.assertEqual([item for item in Path(self.media.name).rglob('*') if item.is_file()],[])

    def test_namespace_collision_and_production_guard_do_not_write(self):
        User.objects.create_user(username="qa000001",full_name="不能覆盖")
        with self.assertRaises(CommandError):
            self.seed()
        self.assertEqual(User.objects.count(),3)
        with override_settings(DEBUG=False),self.assertRaises(CommandError):
            seed_dataset(60)
        with override_settings(DEBUG=False),self.assertRaises(CommandError):
            clear_dataset()

    def test_command_backs_up_once_and_check_is_read_only(self):
        with patch('portal.management.commands.seed_test_data.call_command') as backup:
            call_command('seed_test_data',students=60,date=date(2026,11,17),stdout=io.StringIO())
            call_command('seed_test_data',students=60,stdout=io.StringIO())
            call_command('seed_test_data',check=True,stdout=io.StringIO())
        backup.assert_called_once()
        self.assertEqual(User.objects.filter(groups__name=GROUP,is_staff=False).count(),60)
