from datetime import timedelta
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


def restore_latest():
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


class NotificationMetricsMigrationTests(TransactionTestCase):
    def test_existing_click_counts_and_activity_fields_survive(self):
        before = [('portal', '0013_unique_super_admin')]
        after = [('portal', '0014_notification_click_metrics')]
        self.addCleanup(restore_latest)
        executor = MigrationExecutor(connection); executor.migrate(before)
        old = executor.loader.project_state(before).apps
        user = old.get_model('portal', 'User').objects.create(username='metrics-migration', full_name='统计历史学生')
        visit = old.get_model('portal', 'ContentVisitDaily').objects.create(student=user, kind='faq', object_id=8,
            day=timezone.localdate(), views=7, last_visited_at=timezone.now())
        activity = old.get_model('portal', 'StudentDailyActivity').objects.create(student=user, day=timezone.localdate())
        snapshots = {name: list(old.get_model('portal', name).objects.values()) for name in ['User', 'ContentVisitDaily', 'StudentDailyActivity']}
        executor = MigrationExecutor(connection); executor.migrate(after)
        current = executor.loader.project_state(after).apps
        for name, rows in snapshots.items():
            self.assertEqual(list(current.get_model('portal', name).objects.values()), rows)
        visits = current.get_model('portal', 'ContentVisitDaily')
        notice = visits.objects.create(student_id=user.pk, kind='notification', object_id=8, day=timezone.localdate(), views=3)
        self.assertEqual(visits.objects.get(pk=visit.pk).views, 7)
        self.assertEqual(visits.objects.get(pk=notice.pk).kind, 'notification')
        notice.delete()
        executor = MigrationExecutor(connection); executor.migrate(before)
        restored = executor.loader.project_state(before).apps
        for name, rows in snapshots.items():
            self.assertEqual(list(restored.get_model('portal', name).objects.values()), rows)


class UniqueSuperAdminMigrationTests(TransactionTestCase):
    def test_preserves_accounts_passwords_and_content_while_demoting_extra_qa_root(self):
        before = [('portal', '0012_student_activity_content_visits')]
        after = MigrationExecutor(connection).loader.graph.leaf_nodes('portal')
        self.addCleanup(restore_latest)
        executor = MigrationExecutor(connection); executor.migrate(before)
        old = executor.loader.project_state(before).apps
        user = old.get_model('portal', 'User')
        main = user.objects.create(username='admin', is_superuser=True, is_staff=True, password='keep-main-hash')
        other = user.objects.create(username='qa_root', is_superuser=True, is_staff=True, password='keep-test-hash')
        group = old.get_model('auth', 'Group').objects.create(name='GradConnect QA dataset v1')
        other.groups.add(group)
        faq = old.get_model('portal', 'FAQ').objects.create(question='原问答', answer='原答案', created_by=other, display_order=19)
        original = old.get_model('portal', 'FAQ').objects.values().get(pk=faq.pk)
        executor = MigrationExecutor(connection); executor.migrate(after)
        current = executor.loader.project_state(after).apps
        users = current.get_model('portal', 'User')
        self.assertEqual(users.objects.filter(is_superuser=True).get().pk, main.pk)
        self.assertEqual(users.objects.get(pk=main.pk).password, 'keep-main-hash')
        self.assertEqual(users.objects.get(pk=other.pk).password, 'keep-test-hash')
        self.assertTrue(users.objects.get(pk=other.pk).is_staff)
        self.assertEqual(users.objects.get(pk=other.pk).session_revision, 1)
        self.assertEqual(current.get_model('portal', 'FAQ').objects.values().get(pk=faq.pk), original)


class AnalyticsMigrationTests(TransactionTestCase):
    def test_existing_identity_content_and_history_remain_and_stats_start_empty(self):
        before = [('portal', '0011_content_display_order')]
        after = [('portal', '0014_notification_click_metrics')]
        self.addCleanup(restore_latest)
        executor = MigrationExecutor(connection); executor.migrate(before)
        old = executor.loader.project_state(before).apps
        user = old.get_model('portal', 'User').objects.create(username='analytics-migration', full_name='历史学生', password='original-hash')
        faq = old.get_model('portal', 'FAQ').objects.create(question='历史问答', answer='历史正文', created_by=user, display_order=9)
        snapshots = {'User': list(old.get_model('portal', 'User').objects.values()), 'FAQ': list(old.get_model('portal', 'FAQ').objects.values())}
        executor = MigrationExecutor(connection); executor.migrate(after)
        current = executor.loader.project_state(after).apps
        item = current.get_model('portal', 'User').objects.get(pk=user.pk)
        self.assertIsNone(item.last_seen_at); self.assertIsNone(item.online_until)
        self.assertEqual(current.get_model('portal', 'ContentVisitDaily').objects.count(), 0)
        self.assertEqual(current.get_model('portal', 'StudentDailyActivity').objects.count(), 0)
        for name, rows in snapshots.items():
            new = [{key: value for key, value in row.items() if key not in {'last_seen_at', 'online_until'}} for row in current.get_model('portal', name).objects.values()]
            self.assertEqual(new, rows)
        executor = MigrationExecutor(connection); executor.migrate(before)
        restored = executor.loader.project_state(before).apps
        for name, rows in snapshots.items():
            self.assertEqual(list(restored.get_model('portal', name).objects.values()), rows)


class ContentOrderMigrationTests(TransactionTestCase):
    def test_initial_order_and_all_existing_content_fields_survive(self):
        before=[('portal','0010_user_enrollment_status_alter_cadretenure_ends_on_and_more')]
        after=MigrationExecutor(connection).loader.graph.leaf_nodes('portal')
        self.addCleanup(restore_latest)
        executor=MigrationExecutor(connection);executor.migrate(before)
        old=executor.loader.project_state(before).apps
        user=old.get_model('portal','User').objects.create(username='order-migration',full_name='原管理员',is_staff=True)
        snapshots={}
        for name in ['Notification','ServiceGuide','Policy','FAQ','Video']:
            model=old.get_model('portal',name)
            for index in range(2):
                if name=='Notification':data={'title':f'原通知{index}','body':'原正文','attachment':'old/file.pdf','created_by':user}
                elif name=='ServiceGuide':data={'title':f'原指南{index}','owner':'原负责人','contact':'原电话','office':'原地点','process':'原流程','created_by':user}
                elif name=='Policy':data={'status':'draft','created_by':user}
                elif name=='FAQ':data={'question':f'原问答{index}','answer':'原答复','created_by':user}
                else:data={'title':f'原视频{index}','file':'old/video.mp4','uploaded_by':user}
                model.objects.create(**data)
            field='created_at' if name=='Video' else 'updated_at'
            snapshots[name]=(list(model.objects.order_by('-'+field,'-pk').values_list('pk',flat=True)),list(model.objects.order_by('pk').values()))
        executor=MigrationExecutor(connection);executor.migrate(after);current=executor.loader.project_state(after).apps
        for name,(ids,rows) in snapshots.items():
            model=current.get_model('portal',name)
            self.assertEqual(list(model.objects.values_list('pk',flat=True)),ids)
            values=[{key:value for key,value in row.items() if key!='display_order'} for row in model.objects.order_by('pk').values()]
            self.assertEqual(values,rows)
        executor=MigrationExecutor(connection);executor.migrate(before);restored=executor.loader.project_state(before).apps
        for name,(_,rows) in snapshots.items():self.assertEqual(list(restored.get_model('portal',name).objects.order_by('pk').values()),rows)


class EnrollmentMigrationTests(TransactionTestCase):
    def test_old_dates_accounts_and_cadre_history_survive_status_migration(self):
        from datetime import date
        before = [("portal", "0009_policyrevision_legacy_category_video_legacy_category_and_more")]
        after = MigrationExecutor(connection).loader.graph.leaf_nodes("portal")
        self.addCleanup(restore_latest)
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        old = executor.loader.project_state(before).apps
        user = old.get_model("portal", "User").objects.create(username="old-grad", full_name="原学生", graduation_month=date(2020, 6, 1),
            password="unchanged", is_active=False, session_revision=3)
        tenure = old.get_model("portal", "CadreTenure").objects.create(student=user, position="历史职务", starts_on=date(2019, 1, 1), ends_on=None)
        preview = old.get_model("portal", "StudentImportBatch").objects.create(uploaded_by=user, row_count=1, payload=[{"username": "old"}])
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        current = executor.loader.project_state(after).apps
        item = current.get_model("portal", "User").objects.get(pk=user.pk)
        self.assertEqual((item.enrollment_status, item.graduation_month, item.password, item.is_active, item.session_revision),
                         ("enrolled", date(2020, 6, 1), "unchanged", False, 3))
        self.assertIsNone(current.get_model("portal", "CadreTenure").objects.get(pk=tenure.pk).ends_on)
        self.assertEqual(current.get_model("portal", "StudentImportBatch").objects.get(pk=preview.pk).format_version, 2)
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        restored = executor.loader.project_state(before).apps
        self.assertEqual(restored.get_model("portal", "User").objects.get(pk=user.pk).graduation_month, date(2020, 6, 1))


class CategoryAndApplicationMigrationTests(TransactionTestCase):
    def test_old_content_and_identity_survive_forward_and_category_rollback(self):
        before = [("portal", "0004_serviceguide_guideattachment")]
        after = MigrationExecutor(connection).loader.graph.leaf_nodes("portal")
        self.addCleanup(restore_latest)
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        old = executor.loader.project_state(before).apps
        user = old.get_model("portal", "User").objects.create(username="migration-student", full_name="旧账号姓名")
        originals = ["日常事务", "后勤", "后勤及校园管理", "academic", "旧的自定义分类" * 5, "常见问题"]
        faq_ids = [old.get_model("portal", "FAQ").objects.create(question=f"历史问题{i}", answer="历史正文", category=category,
                   status="published", created_by=user).pk for i, category in enumerate(originals)]
        conversation = old.get_model("portal", "Conversation").objects.create(student=user, subject="历史私人咨询")
        leave = old.get_model("portal", "Leave").objects.create(student=user, kind="personal", starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(days=1), reason="历史原因", destination="历史去向", contact="123456")
        unnamed = old.get_model("portal", "User").objects.create(username="x" * 150, full_name="")
        unnamed_leave = old.get_model("portal", "Leave").objects.create(student=unnamed, kind="personal", starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(days=1), reason="旧记录", destination="原去向", contact="123456")
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        current = executor.loader.project_state(after).apps
        faq = current.get_model("portal", "FAQ")
        self.assertEqual([faq.objects.get(pk=pk).category for pk in faq_ids], ["daily", "campus", "campus", "academic", "other", "other"])
        self.assertEqual([faq.objects.get(pk=pk).legacy_category for pk in faq_ids], originals)
        self.assertEqual(faq.objects.filter(answer="历史正文", status="published").count(), 6)
        self.assertEqual(current.get_model("portal", "Conversation").objects.get(pk=conversation.pk).category, "other")
        migrated_leave = current.get_model("portal", "Leave").objects.get(pk=leave.pk)
        self.assertEqual((migrated_leave.student_name, migrated_leave.student_number), ("旧账号姓名", "migration-student"))
        self.assertEqual((migrated_leave.destination, migrated_leave.province, migrated_leave.city, migrated_leave.tutor_signature), ("历史去向", "", "", ""))
        unknown_name = current.get_model("portal", "Leave").objects.get(pk=unnamed_leave.pk)
        self.assertEqual((unknown_name.student_name, unknown_name.student_number), ("", "x" * 150))
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        restored = executor.loader.project_state(before).apps.get_model("portal", "FAQ")
        self.assertEqual([restored.objects.get(pk=pk).category for pk in faq_ids], originals)


class MonthlyAttendanceMigrationTests(TransactionTestCase):
    def test_daily_history_becomes_one_monthly_row_and_originals_are_preserved(self):
        from datetime import date
        from django.core.exceptions import ValidationError
        from .services import confirm_attendance
        before = [("portal", "0006_leave_city_leave_province_leave_student_name_and_more")]
        after = MigrationExecutor(connection).loader.graph.leaf_nodes("portal")
        self.addCleanup(restore_latest)
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        old = executor.loader.project_state(before).apps
        User = old.get_model("portal", "User")
        admin = User.objects.create(username="migration-admin", full_name="管理员", is_staff=True)
        student = User.objects.create(username="00001", full_name="历史学生甲")
        other = User.objects.create(username="00002", full_name="历史学生乙")
        unnamed = User.objects.create(username="x" * 150, full_name="")
        batch = old.get_model("portal", "AttendanceBatch").objects.create(month=date(2026, 11, 1), uploaded_by=admin, row_count=7, is_confirmed=True)
        pending = old.get_model("portal", "AttendanceBatch").objects.create(month=date(2026, 11, 1), uploaded_by=admin, row_count=1, payload=[{"date": "2026-11-10"}])
        Daily = old.get_model("portal", "Attendance")
        for day, status in [(1, "normal"), (2, "late"), (3, "excused"), (4, "absent")]:
            Daily.objects.create(student=student, date=date(2026, 11, day), status=status, batch=batch, note="历史备注保留")
        Daily.objects.create(student=student, date=date(2025, 12, 31), status="normal", batch=batch)
        Daily.objects.create(student=other, date=date(2026, 11, 1), status="excused", batch=batch)
        Daily.objects.create(student=unnamed, date=date(2026, 11, 1), status="normal", batch=batch)
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        current = executor.loader.project_state(after).apps
        Monthly = current.get_model("portal", "MonthlyAttendance")
        self.assertEqual(Monthly.objects.count(), 4)
        item = Monthly.objects.get(student_id=student.pk, month=date(2026, 11, 1))
        self.assertEqual((item.student_name, item.student_number, item.clock_days, item.leave_days), ("历史学生甲", "00001", 2, 1))
        self.assertEqual(Monthly.objects.get(student_id=student.pk, month=date(2025, 12, 1)).clock_days, 1)
        self.assertEqual(Monthly.objects.get(student_id=other.pk).leave_days, 1)
        self.assertEqual(Monthly.objects.get(student_id=unnamed.pk).student_name, "")
        self.assertEqual(current.get_model("portal", "Attendance").objects.count(), 7)
        self.assertEqual(current.get_model("portal", "AttendanceBatch").objects.get(pk=pending.pk).format_version, 1)
        current_admin = current.get_model("portal", "User").objects.get(pk=admin.pk)
        with self.assertRaisesMessage(ValidationError, "旧版每日打卡预览"):
            confirm_attendance(pending.pk, current_admin)
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        self.assertEqual(executor.loader.project_state(before).apps.get_model("portal", "Attendance").objects.count(), 7)


class StudentInformationMigrationTests(TransactionTestCase):
    def test_existing_accounts_passwords_history_and_previews_survive_without_fabricated_fields(self):
        before = [("portal", "0007_attendancebatch_format_version_and_more")]
        after = MigrationExecutor(connection).loader.graph.leaf_nodes("portal")
        self.addCleanup(restore_latest)
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        old = executor.loader.project_state(before).apps
        admin = old.get_model("portal", "User").objects.create(username="migration-admin", full_name="管理员", is_staff=True)
        student = old.get_model("portal", "User").objects.create(username="old-student", full_name="旧学生姓名", password="original-hash", department="原院系班级")
        conversation = old.get_model("portal", "Conversation").objects.create(student=student, subject="旧私人咨询")
        preview = old.get_model("portal", "StudentImportBatch").objects.create(uploaded_by=admin, row_count=1, payload=[{"username": "pending-old"}])
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        new = executor.loader.project_state(after).apps
        item = new.get_model("portal", "User").objects.get(pk=student.pk)
        self.assertEqual((item.full_name, item.password, item.department, item.is_active), ("旧学生姓名", "original-hash", "原院系班级", True))
        self.assertEqual((item.id_number, item.party_branch, item.student_class, item.political_status), ("", "", "", ""))
        self.assertIsNone(item.graduation_month)
        self.assertFalse(item.is_archived)
        self.assertTrue(new.get_model("portal", "Conversation").objects.filter(pk=conversation.pk, student_id=student.pk).exists())
        self.assertEqual(new.get_model("portal", "StudentImportBatch").objects.get(pk=preview.pk).format_version, 1)


class ResourceCategoryMigrationTests(TransactionTestCase):
    def test_category_mapping_preserves_versions_files_status_and_original_labels_on_rollback(self):
        before = [("portal", "0008_studentimportbatch_format_version_and_more")]
        after = MigrationExecutor(connection).loader.graph.leaf_nodes("portal")
        self.addCleanup(restore_latest)
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        old = executor.loader.project_state(before).apps
        admin = old.get_model("portal", "User").objects.create(username="category-admin", full_name="管理员", is_staff=True)
        policy = old.get_model("portal", "Policy").objects.create(created_by=admin, status="published")
        labels = ["校纪校规", "旧自定义" * 10, "教务", ""]
        revisions = [old.get_model("portal", "PolicyRevision").objects.create(policy=policy, number=index + 1,
            title=f"原制度版本{index}", body="原正文不变", category=label, is_draft=index == 1,
            attachment="policyrevision/original.pdf") for index, label in enumerate(labels)]
        policy.published_revision = revisions[0]
        policy.save()
        video_labels = ["党建", "班团建设", "旧类别" * 15, "", "party"]
        videos = [old.get_model("portal", "Video").objects.create(title=f"原视频{index}", category=label,
            description="原视频介绍", file=f"video/original-{index}.mp4", uploaded_by=admin, is_deleted=index == 1)
            for index, label in enumerate(video_labels)]
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        new = executor.loader.project_state(after).apps
        Revision = new.get_model("portal", "PolicyRevision")
        Video = new.get_model("portal", "Video")
        self.assertEqual([Revision.objects.get(pk=item.pk).category for item in revisions], ["discipline", "other", "academic", "other"])
        self.assertEqual([Revision.objects.get(pk=item.pk).legacy_category for item in revisions], labels)
        self.assertEqual([Video.objects.get(pk=item.pk).category for item in videos], ["party", "class_group", "other", "other", "party"])
        self.assertEqual([Video.objects.get(pk=item.pk).legacy_category for item in videos], video_labels)
        self.assertEqual(new.get_model("portal", "Policy").objects.get(pk=policy.pk).published_revision_id, revisions[0].pk)
        self.assertTrue(Revision.objects.get(pk=revisions[1].pk).is_draft)
        self.assertEqual(Revision.objects.filter(body="原正文不变", attachment="policyrevision/original.pdf").count(), 4)
        self.assertTrue(Video.objects.get(pk=videos[1].pk).is_deleted)
        self.assertEqual(Video.objects.get(pk=videos[0].pk).file.name, "video/original-0.mp4")
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        restored = executor.loader.project_state(before).apps
        self.assertEqual([restored.get_model("portal", "PolicyRevision").objects.get(pk=item.pk).category for item in revisions], labels)
        self.assertEqual([restored.get_model("portal", "Video").objects.get(pk=item.pk).category for item in videos], video_labels)
