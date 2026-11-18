from datetime import timedelta
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from portal.models import (MonthlyAttendance, AttendanceBatch, Conversation, FAQ, Leave, LeaveEvent,
                           Message, Policy, PolicyRevision, Publication, ServiceGuide, User)


class Command(BaseCommand):
    help = "仅开发环境：创建可重复运行的演示账号及示例数据，不覆盖已有账号密码。"

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("演示数据仅允许在 DJANGO_DEBUG=1 的本地开发环境创建。")
        admin, new = User.objects.get_or_create(username="admin", defaults={"full_name": "管理员", "is_staff": True,
            "is_superuser": not User.objects.filter(is_superuser=True).exists()})
        if new:
            admin.set_password("GradDemo!2026")
            admin.save()
        students = []
        for username, name in [("20260001", "林同学"), ("20260002", "陈同学")]:
            user, new = User.objects.get_or_create(username=username, defaults={"full_name": name, "department": "计算机学院 · 2026 级"})
            if new:
                user.set_password("StudentDemo!2026")
                user.save()
            students.append(user)
        if not Policy.objects.exists():
            for title, category, body in [
                ("研究生请销假办理指南（示例）", "daily", "请假前请填写起止时间、离校去向、联系电话和请假原因。\n\n申请提交后由管理员审核。待审核时可撤销；批准后请按照线下要求办理销假，由管理员线上确认。\n\n本内容仅供功能演示，正式使用时请替换为学校有效制度。"),
                ("研究生校园事务服务指引（示例）", "daily", "学生可以通过平台查询官方问答、查阅办事指南，并发起私人咨询。\n\n私人咨询内容仅本人及管理员可见。具体办理要求请以学校正式通知为准。"),
                ("学习资源使用说明（示例）", "other", "在学习视频栏目检索课程并在线播放。如遇播放问题，请通过咨询模块联系管理员。"),
            ]:
                policy = Policy.objects.create(created_by=admin)
                revision = PolicyRevision.objects.create(policy=policy, title=title, category=category, body=body, summary="演示内容，用于熟悉服务流程；请以正式发布的学校制度为准。", is_draft=False)
                policy.published_revision = revision
                policy.status = Publication.PUBLISHED
                policy.save()
        if not FAQ.objects.exists():
            for question, answer in [("忘记登录密码怎么办？", "请线下联系管理员核实身份。管理员重置密码后，使用新密码重新登录。"), ("请假获批后如何销假？", "请按要求在线下办理销假，随后由管理员在线确认。可在申请详情中查看办理状态。"), ("为什么看不到某个月的打卡记录？", "请先核对查询月份。如仍无记录，可能尚未导入，请通过私人咨询联系管理员。")]:
                FAQ.objects.create(question=question, answer=answer, status=Publication.PUBLISHED, created_by=admin)
        if not ServiceGuide.objects.exists():
            ServiceGuide.objects.create(title="请销假办理流程（示例）", category="daily", owner="所在院系管理员",
                contact="请线下联系所在院系管理员", office="以所在院系通知为准",
                process="1. 在请假与销假模块填写申请。\n2. 等待管理员审核，查看审核结果。\n3. 获批后按要求办理线下销假，由管理员在线确认。\n\n本内容仅用于演示，正式流程以学校发布的指南为准。",
                created_by=admin, updated_by=admin)
        now = timezone.now()
        if not Leave.objects.exists():
            leave = Leave.objects.create(student=students[0], kind="personal", starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=2), reason="处理个人事务（演示申请）。", destination="本市", contact="13800000000")
            LeaveEvent.objects.create(leave=leave, actor=students[0], status="pending", note="提交请假申请")
        if not Conversation.objects.exists():
            conversation = Conversation.objects.create(student=students[0], subject="关于打卡记录查询（示例）", awaiting_reply=False)
            Message.objects.create(conversation=conversation, author=students[0], body="请问在哪里查看本月的打卡记录？")
            Message.objects.create(conversation=conversation, author=admin, body="请进入“月度打卡”，选择月份后查询，即可查看本人每月汇总。")
        if not MonthlyAttendance.objects.exists():
            today = timezone.localdate()
            batch = AttendanceBatch.objects.create(month=today.replace(day=1), uploaded_by=admin, row_count=len(students), is_confirmed=True)
            for student in students:
                MonthlyAttendance.objects.create(student=student, student_name=student.full_name, student_number=student.username,
                    month=today.replace(day=1), clock_days=min(today.day, 10), leave_days=0, batch=batch)
        self.stdout.write(self.style.SUCCESS("演示数据已就绪（已有账号密码不会被覆盖）。"))
        self.stdout.write("新建管理员：admin / GradDemo!2026")
        self.stdout.write("新建学生：20260001、20260002 / StudentDemo!2026")
        self.stdout.write("仅限本地演示。生产环境使用独立数据库和自行创建的正式账号。")
