from pathlib import Path
from uuid import uuid4
from datetime import date, timedelta
from django.conf import settings
from django.core.exceptions import ValidationError
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import salted_hmac


def private_upload(instance, filename):
    return f"{instance._meta.model_name}/{uuid4().hex}{Path(filename).suffix.lower()}"


class User(AbstractUser):
    class EnrollmentStatus(models.TextChoices):
        ENROLLED = "enrolled", "在校"
        GRADUATED = "graduated", "毕业"

    class Politics(models.TextChoices):
        MASS = "mass", "群众"
        LEAGUE = "league", "共青团员"
        ACTIVIST = "activist", "共青团员（入党积极分子）"
        CANDIDATE = "candidate", "共青团员（发展对象）"
        PROBATIONARY = "probationary", "预备党员"
        PARTY = "party", "正式党员"

    full_name = models.CharField("姓名", max_length=80)
    department = models.CharField("院系 / 班级", max_length=100, blank=True)
    id_number = models.CharField("身份证号", max_length=18, blank=True, default="")
    graduation_month = models.DateField("历史毕业年月", null=True, blank=True, db_index=True, editable=False)
    enrollment_status = models.CharField("在校状态", max_length=10, choices=EnrollmentStatus,
        default=EnrollmentStatus.ENROLLED, db_index=True)
    party_branch = models.CharField("所在党支部", max_length=100, blank=True, default="", db_index=True)
    student_class = models.CharField("所在班级", max_length=100, blank=True, default="", db_index=True)
    political_status = models.CharField("政治面貌", max_length=20, choices=Politics, blank=True, default="", db_index=True)
    is_archived = models.BooleanField("已归档", default=False, db_index=True)
    session_revision = models.PositiveIntegerField(default=0, editable=False)
    password_change_required = models.BooleanField("须修改初始密码", default=False, editable=False)
    last_seen_at = models.DateTimeField("最近活跃时间", null=True, blank=True, editable=False)
    online_until = models.DateTimeField("在线状态有效期", null=True, blank=True, editable=False, db_index=True)

    class Meta(AbstractUser.Meta):
        abstract = False
        constraints = [
            models.UniqueConstraint(fields=['is_superuser'], condition=Q(is_superuser=True), name='one_super_admin'),
            models.CheckConstraint(condition=Q(is_superuser=False) | Q(is_staff=True, is_active=True, is_archived=False), name='super_admin_available'),
        ]

    def clean(self):
        super().clean()
        if self.is_superuser:
            if not self.is_staff or not self.is_active or self.is_archived:
                raise ValidationError('超级管理员必须保持管理员权限和启用状态，不能归档。')
            if User.objects.filter(is_superuser=True).exclude(pk=self.pk).exists():
                raise ValidationError('系统仅允许一个超级管理员，请使用现有超级管理员账号。')

    def save(self, *args, **kwargs):
        flags = {'is_superuser', 'is_staff', 'is_active', 'is_archived'}
        fields = kwargs.get('update_fields')
        if fields is None or flags.intersection(fields):
            previous = User.objects.filter(pk=self.pk).values('is_superuser').first() if not self._state.adding else None
            if previous and previous['is_superuser'] and (not self.is_superuser or not self.is_staff or not self.is_active or self.is_archived):
                raise ValidationError('唯一超级管理员不能降权、停用或归档。')
            if self.is_superuser:
                self.clean()
        return super().save(*args, **kwargs)

    @property
    def presence(self):
        if self.is_archived:
            return {"key": "restricted", "label": "已归档 / 已停用"}
        if not self.is_active:
            return {"key": "restricted", "label": "账号已停用"}
        if self.enrollment_status == self.EnrollmentStatus.GRADUATED:
            return {"key": "restricted", "label": "已毕业"}
        now = timezone.now()
        online = self.online_until is not None and now < self.online_until <= now + timedelta(minutes=5)
        return {"key": "online" if online else "offline", "label": "在线（最近5分钟有操作）" if online else "离线"}

    def _get_session_auth_hash(self, secret=None):
        if not self.session_revision:
            return super()._get_session_auth_hash(secret=secret)
        return salted_hmac("django.contrib.auth.models.AbstractBaseUser.get_session_auth_hash",
                           f"{self.password}:{self.session_revision}", secret=secret, algorithm="sha256").hexdigest()

    @property
    def masked_id_number(self):
        return self.id_number[:6] + "********" + self.id_number[-4:] if self.id_number else "待补全"

    @property
    def profile_complete(self):
        return all([self.username, self.full_name, self.id_number, self.enrollment_status, self.student_class])

    @property
    def display_name(self):
        return self.full_name or self.username

    def __str__(self):
        return f"{self.username} · {self.display_name}"


class Timestamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        abstract = True


class CadreTenure(Timestamped):
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="cadre_tenures")
    position = models.CharField("任职职务", max_length=100)
    starts_on = models.DateField("开始日期")
    ends_on = models.DateField("结束日期", null=True, blank=True, default=date(2999, 12, 31))

    @property
    def is_current(self):
        today = timezone.localdate()
        return self.starts_on <= today and (self.ends_on is None or self.ends_on >= today)

    class Meta:
        ordering = ["-starts_on", "-pk"]
        constraints = [models.CheckConstraint(condition=Q(ends_on__isnull=True) | Q(ends_on__gte=models.F("starts_on")),
                                              name="cadre_tenure_dates_in_order")]


class Publication(models.TextChoices):
    DRAFT = "draft", "草稿"
    PUBLISHED = "published", "已发布"
    WITHDRAWN = "withdrawn", "已撤销"


class Notification(Timestamped):
    display_order = models.PositiveBigIntegerField(default=0, db_index=True, editable=False)
    class Category(models.TextChoices):
        ADMISSION = "admission", "新生入学"
        PARTY = "party", "党建"
        RECRUITMENT = "recruitment", "招聘信息"
        AWARDS = "awards", "评奖评优"
        COMPETITION = "competition", "竞赛"
        CULTURE_SPORTS = "culture_sports", "文体活动"
        DEPARTURE = "departure", "离校手续"
        OTHER = "other", "其他"

    class Status(models.TextChoices):
        PUBLISHED = "published", "已发布"
        WITHDRAWN = "withdrawn", "已撤下"

    title = models.CharField("通知标题", max_length=200)
    category = models.CharField("通知类别", max_length=20, choices=Category, default=Category.OTHER, db_index=True)
    summary = models.CharField("通知摘要", max_length=300, blank=True)
    body = models.TextField("通知正文", max_length=20000, blank=True)
    attachment = models.FileField("PDF 附件", upload_to=private_upload, blank=True)
    status = models.CharField(max_length=12, choices=Status, default=Status.PUBLISHED, db_index=True)
    published_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_notifications")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="updated_notifications")

    class Meta:
        ordering = ["-display_order", "-updated_at", "-pk"]


class Policy(Timestamped):
    display_order = models.PositiveBigIntegerField(default=0, db_index=True, editable=False)
    status = models.CharField(max_length=12, choices=Publication, default=Publication.DRAFT, db_index=True)
    published_revision = models.ForeignKey("PolicyRevision", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    class Meta:
        ordering = ["-display_order", "-updated_at", "-pk"]


class PolicyRevision(Timestamped):
    class Category(models.TextChoices):
        DISCIPLINE = "discipline", "校纪校规"
        DAILY = "daily", "日常事务"
        ACADEMIC = "academic", "教务"
        AWARDS = "awards", "评奖评优"
        COMPETITION = "competition", "竞赛"
        PARTY = "party", "党建"
        SAFETY = "safety", "后勤管理及校园安全"
        OTHER = "other", "其他"

    policy = models.ForeignKey(Policy, on_delete=models.CASCADE, related_name="revisions")
    number = models.PositiveIntegerField(default=1)
    title = models.CharField("制度标题", max_length=200)
    category = models.CharField("制度类别", max_length=20, choices=Category, default=Category.OTHER, db_index=True)
    legacy_category = models.CharField("迁移前分类", max_length=60, null=True, blank=True, editable=False)
    summary = models.CharField("摘要", max_length=300, blank=True)
    body = models.TextField("正文", blank=True)
    attachment = models.FileField("PDF 附件", upload_to=private_upload, blank=True)
    is_draft = models.BooleanField(default=True)
    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(fields=["policy", "number"], name="unique_policy_version"),
            models.UniqueConstraint(fields=["policy"], condition=Q(is_draft=True), name="one_policy_draft"),
        ]


class Leave(Timestamped):
    class Status(models.TextChoices):
        PENDING = "pending", "待审核"
        APPROVED = "approved", "已批准 · 待销假"
        REJECTED = "rejected", "已驳回"
        WITHDRAWN = "withdrawn", "已撤销"
        CLOSED = "closed", "已销假"
    class Kind(models.TextChoices):
        PERSONAL = "personal", "事假"
        SICK = "sick", "病假"
        OTHER = "other", "其他"
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="leaves")
    kind = models.CharField("请假类型", max_length=12, choices=Kind)
    starts_at = models.DateTimeField("开始时间")
    ends_at = models.DateTimeField("结束时间")
    reason = models.TextField("请假原因", max_length=3000)
    destination = models.CharField("离校去向", max_length=200)
    contact = models.CharField("联系电话", max_length=40)
    student_name = models.CharField("姓名", max_length=80, blank=True, default="", editable=False)
    student_number = models.CharField("学号", max_length=150, blank=True, default="", editable=False)
    province = models.CharField("去往省份 / 直辖市", max_length=40, blank=True, default="")
    city = models.CharField("去往城市", max_length=40, blank=True, default="")
    tutor_signature = models.CharField("是否导师本人线下签字", max_length=3, blank=True, default="",
        choices=[("yes", "是：导师本人线下签字"), ("no", "否：使用电子签名或代签，已征得导师同意")])
    status = models.CharField(max_length=12, choices=Status, default=Status.PENDING, db_index=True)
    previous = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="resubmissions")
    class Meta:
        ordering = ["-created_at"]
        constraints = [models.CheckConstraint(condition=Q(ends_at__gt=models.F("starts_at")), name="leave_end_after_start")]


class LeaveEvent(models.Model):
    leave = models.ForeignKey(Leave, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    status = models.CharField(max_length=12, choices=Leave.Status)
    note = models.TextField(blank=True, max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["created_at", "pk"]


class Registration(Timestamped):
    class Kind(models.TextChoices):
        INTERNSHIP = "internship", "实习登记"
        RENTAL = "rental", "校外租房"

    class Status(models.TextChoices):
        PENDING = "pending", "待审核"
        APPROVED = "approved", "已通过"
        REJECTED = "rejected", "已驳回"
        WITHDRAWN = "withdrawn", "已撤回"

    class RentalReason(models.TextChoices):
        RESEARCH = "research", "科研需求"
        HEALTH = "health", "身体原因"
        OTHER = "other", "其他"

    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="registrations")
    student_name = models.CharField("姓名", max_length=80)
    student_number = models.CharField("学号", max_length=150)
    kind = models.CharField(max_length=12, choices=Kind, db_index=True)
    starts_at = models.DateTimeField("开始时间")
    ends_at = models.DateTimeField("结束时间")
    employer = models.CharField("实习单位", max_length=200, blank=True)
    position = models.CharField("实习岗位", max_length=200, blank=True)
    address = models.CharField("详细地址", max_length=400)
    contact = models.CharField("本人联系方式", max_length=100)
    emergency_name = models.CharField("紧急联系人", max_length=80)
    emergency_contact = models.CharField("紧急联系方式", max_length=100)
    rental_reason = models.CharField("租房原因", max_length=12, choices=RentalReason, blank=True)
    reason_detail = models.TextField("其他原因说明", max_length=3000, blank=True)
    has_dorm = models.BooleanField("是否有校内宿舍", null=True, blank=True)
    dorm_number = models.CharField("校内宿舍号", max_length=100, blank=True)
    status = models.CharField(max_length=12, choices=Status, default=Status.PENDING, db_index=True)

    previous = models.OneToOneField("self", on_delete=models.RESTRICT, null=True, blank=True,
        related_name="resubmission", editable=False, verbose_name="原申请")

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.CheckConstraint(condition=Q(ends_at__gt=models.F("starts_at")), name="registration_end_after_start")]


class RegistrationAttachment(models.Model):
    registration = models.ForeignKey(Registration, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=private_upload)
    original_name = models.CharField(max_length=255)

    class Meta:
        ordering = ["pk"]


class RegistrationEvent(models.Model):
    registration = models.ForeignKey(Registration, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    status = models.CharField(max_length=12, choices=Registration.Status)
    note = models.TextField(max_length=2000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]


class ServiceGuide(Timestamped):
    display_order = models.PositiveBigIntegerField(default=0, db_index=True, editable=False)
    class Category(models.TextChoices):
        DAILY = "daily", "日常事务"
        AWARDS = "awards", "评奖评优"
        ADMISSION = "admission", "新生入学"
        DEPARTURE = "departure", "离校手续"
        PARTY = "party", "党建"
        LOGISTICS = "logistics", "后勤"
        ACADEMIC = "academic", "教务"
        OTHER = "other", "其他"

    class Status(models.TextChoices):
        PUBLISHED = "published", "已发布"
        WITHDRAWN = "withdrawn", "已撤下"

    title = models.CharField("指南标题", max_length=200)
    category = models.CharField("指南类别", max_length=20, choices=Category, default=Category.DAILY, db_index=True)
    owner = models.CharField("负责人", max_length=80)
    contact = models.CharField("联系方式", max_length=200)
    office = models.CharField("办公地址", max_length=300)
    process = models.TextField("办理流程", max_length=30000)
    status = models.CharField(max_length=12, choices=Status, default=Status.PUBLISHED, db_index=True)
    published_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_guides")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="updated_guides")

    class Meta:
        ordering = ["-display_order", "-updated_at", "-pk"]

    def __str__(self):
        return self.title


class GuideAttachment(models.Model):
    guide = models.ForeignKey(ServiceGuide, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=private_upload)
    original_name = models.CharField(max_length=255)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    @property
    def extension(self):
        return Path(self.file.name).suffix.lower()

    @property
    def is_pdf(self):
        return self.extension == ".pdf"

    def __str__(self):
        return self.original_name

    class Meta:
        ordering = ["pk"]


class Matter(Timestamped):
    # 已停用的模块，仅保留历史数据和旧咨询的关联，不再提供页面或新增入口。
    title = models.CharField("事项标题", max_length=200)
    category = models.CharField("分类", max_length=60, default="学生事务")
    description = models.TextField("事项说明")
    status = models.CharField(max_length=12, choices=Publication, default=Publication.DRAFT, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    class Meta:
        ordering = ["-updated_at"]


class MatterUpdate(models.Model):
    matter = models.ForeignKey(Matter, on_delete=models.CASCADE, related_name="updates")
    content = models.TextField("进展内容", max_length=5000)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-created_at", "-pk"]


class InquiryCategory(models.TextChoices):
    DAILY = "daily", "日常事务"
    AWARDS = "awards", "评奖评优"
    ADMISSION = "admission", "新生入学"
    DEPARTURE = "departure", "离校手续"
    PARTY = "party", "党建"
    CAMPUS = "campus", "后勤及校园管理"
    ACADEMIC = "academic", "教务"
    OTHER = "other", "其他"


class Conversation(Timestamped):
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="conversations")
    matter = models.ForeignKey(Matter, null=True, blank=True, on_delete=models.PROTECT, related_name="conversations")
    subject = models.CharField("咨询主题", max_length=200)
    category = models.CharField("咨询类别", max_length=20, choices=InquiryCategory, default=InquiryCategory.OTHER, db_index=True)
    awaiting_reply = models.BooleanField(default=True, db_index=True)
    class Meta:
        ordering = ["-updated_at"]


class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    body = models.TextField("咨询内容", max_length=5000)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["created_at", "pk"]


class InboxNotice(models.Model):
    class Kind(models.TextChoices):
        CONSULTATION = "consultation", "咨询回复"
        LEAVE = "leave", "请销假"
        INTERNSHIP = "internship", "实习登记"
        RENTAL = "rental", "校外租房"

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="inbox_notices")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    kind = models.CharField(max_length=16, choices=Kind, db_index=True)
    object_id = models.PositiveBigIntegerField()
    event_key = models.CharField(max_length=100)
    title = models.CharField(max_length=160)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["recipient", "read_at", "-id"], name="inbox_recipient_unread")]
        constraints = [models.UniqueConstraint(fields=["recipient", "event_key"], name="inbox_unique_event")]


class FAQ(Timestamped):
    display_order = models.PositiveBigIntegerField(default=0, db_index=True, editable=False)
    question = models.CharField("问题", max_length=200)
    answer = models.TextField("官方解答", max_length=10000)
    category = models.CharField("问答类别", max_length=20, choices=InquiryCategory, default=InquiryCategory.OTHER, db_index=True)
    legacy_category = models.CharField("迁移前分类", max_length=60, blank=True, default="", editable=False)
    status = models.CharField(max_length=12, choices=Publication, default=Publication.DRAFT, db_index=True)
    source = models.ForeignKey(Conversation, null=True, blank=True, on_delete=models.SET_NULL, related_name="faqs")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    class Meta:
        ordering = ["-display_order", "-updated_at", "-pk"]


class Video(Timestamped):
    display_order = models.PositiveBigIntegerField(default=0, db_index=True, editable=False)
    class Category(models.TextChoices):
        PARTY = "party", "党建"
        CLASS_GROUP = "class_group", "班团"
        OTHER = "other", "其他"

    title = models.CharField("视频标题", max_length=200)
    description = models.TextField("内容介绍", blank=True, max_length=5000)
    category = models.CharField("视频类别", max_length=20, choices=Category, default=Category.OTHER, db_index=True)
    legacy_category = models.CharField("迁移前分类", max_length=60, null=True, blank=True, editable=False)
    file = models.FileField("MP4 视频", upload_to=private_upload)
    is_deleted = models.BooleanField(default=False, db_index=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    class Meta:
        ordering = ["-display_order", "-created_at", "-pk"]


class AttendanceBatch(models.Model):
    month = models.DateField(null=True, blank=True)
    format_version = models.PositiveSmallIntegerField(default=2)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    row_count = models.PositiveIntegerField()
    payload = models.JSONField(default=list, blank=True)
    is_confirmed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-created_at"]


class StudentImportBatch(models.Model):
    format_version = models.PositiveSmallIntegerField(default=3)
    mode = models.CharField(max_length=10, choices=[("create", "新增学生"), ("update", "更新已有学生资料")], default="create")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    row_count = models.PositiveIntegerField()
    # 仅保存校验后的身份字段和密码哈希，原始 CSV 与明文密码均不保存。
    payload = models.JSONField(default=list, blank=True)
    is_confirmed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class StudentRosterBatch(models.Model):
    class Kind(models.TextChoices):
        GRADUATE = "graduate", "批量修改毕业学生"
        TENURE = "tenure", "导入履职信息"

    kind = models.CharField(max_length=10, choices=Kind)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    row_count = models.PositiveIntegerField()
    payload = models.JSONField(default=list, blank=True)
    is_confirmed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class Attendance(Timestamped):
    # 历史每日明细只用于保留旧数据，新页面和导入使用 MonthlyAttendance。
    class Status(models.TextChoices):
        NORMAL = "normal", "正常"
        LATE = "late", "迟到"
        ABSENT = "absent", "缺卡"
        EXCUSED = "excused", "请假"
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="attendance")
    date = models.DateField("日期", db_index=True)
    status = models.CharField("状态", max_length=10, choices=Status)
    note = models.CharField("备注", max_length=300, blank=True)
    batch = models.ForeignKey(AttendanceBatch, on_delete=models.PROTECT)
    class Meta:
        ordering = ["-date", "student__username"]
        constraints = [models.UniqueConstraint(fields=["student", "date"], name="one_attendance_per_day")]


class MonthlyAttendance(Timestamped):
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="monthly_attendance")
    student_name = models.CharField("姓名", max_length=80)
    student_number = models.CharField("学号", max_length=150)
    month = models.DateField("月份", db_index=True)
    clock_days = models.PositiveSmallIntegerField("打卡天数")
    leave_days = models.PositiveSmallIntegerField("请假天数")
    batch = models.ForeignKey(AttendanceBatch, on_delete=models.PROTECT, related_name="monthly_records")

    @property
    def total_days(self):
        return self.clock_days + self.leave_days

    class Meta:
        ordering = ["-month", "student_number", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["student", "month"], name="one_attendance_per_month"),
            models.CheckConstraint(condition=Q(month__day=1), name="attendance_month_first_day"),
            models.CheckConstraint(condition=Q(clock_days__lte=31) & Q(leave_days__lte=models.Value(31) - models.F("clock_days")),
                                   name="attendance_total_at_most_31"),
        ]


class Audit(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    action = models.CharField(max_length=120)
    target = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-created_at"]


class LoginGuard(models.Model):
    key = models.CharField(max_length=64, unique=True)
    failures = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField()


class StudentDailyActivity(models.Model):
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="activity_days")
    day = models.DateField("活跃日期", db_index=True)
    first_seen_at = models.DateTimeField("首次活跃时间", default=timezone.now)
    last_seen_at = models.DateTimeField("最近活跃时间", default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["student", "day"], name="unique_student_activity_day")]


class ContentVisitDaily(models.Model):
    class Kind(models.TextChoices):
        NOTIFICATION = "notification", "通知汇总"
        POLICY = "policy", "规章制度"
        FAQ = "faq", "官方问答"
        GUIDE = "guide", "办事指南"
        VIDEO = "video", "学习视频"

    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="content_visits")
    kind = models.CharField("内容类型", max_length=20, choices=Kind)
    object_id = models.PositiveBigIntegerField("内容编号")
    day = models.DateField("访问日期", db_index=True)
    views = models.PositiveIntegerField("点击量", default=0)
    last_visited_at = models.DateTimeField("最近点击时间", null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["student", "kind", "object_id", "day"], name="unique_student_content_day")]
        indexes = [models.Index(fields=["kind", "object_id", "day"], name="content_visit_lookup")]

