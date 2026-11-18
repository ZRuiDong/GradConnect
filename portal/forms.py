from pathlib import Path
import re
from datetime import date
from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from .models import User, CadreTenure, PolicyRevision, Leave, FAQ, Video, Notification, ServiceGuide, GuideAttachment, InquiryCategory, Registration
from .uploads import (GUIDE_MAX_ATTACHMENTS, validate_guide_upload, REGISTRATION_FILE_TYPES,
                      validate_registration_upload, NOTIFICATION_FILE_TYPES, validate_notification_upload)
from .widgets import DropdownCheckboxSelect


def validate_upload(value, kind):
    if not isinstance(value, UploadedFile):
        return value
    limit = 20 if kind == "pdf" else 500
    if value.size > limit * 1024 * 1024:
        raise ValidationError(f"文件大小不能超过 {limit} MB。")
    if Path(value.name).suffix.lower() != f".{kind}":
        raise ValidationError(f"请上传 {kind.upper()} 格式文件。")
    head = value.read(16)
    value.seek(0)
    if kind == "pdf" and not head.startswith(b"%PDF-"):
        raise ValidationError("文件内容不是有效的 PDF。")
    if kind == "mp4" and head[4:8] != b"ftyp":
        raise ValidationError("文件内容不是有效的 MP4 容器。")
    return value


class LoginForm(AuthenticationForm):
    username = forms.CharField(label="学号 / 管理员账号", max_length=150,
                               widget=forms.TextInput(attrs={"autofocus": True, "autocomplete": "username"}))
    password = forms.CharField(label="密码", strip=False,
                               widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))
    error_messages = {"invalid_login": "账号或密码不正确，请重新输入。", "inactive": "账号已停用，请联系管理员。"}


class StudentProfileMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ["full_name", "username", "id_number", "student_class"]:
            self.fields[name].required = True
        self.fields["id_number"].help_text = "填写18位居民身份证号，末位 X 不区分大小写。"
        self.fields["party_branch"].label = "所在党支部（选填）"
        self.fields["party_branch"].help_text = "没有所属党支部时可留空，也可填写“无”。"
        self.fields["department"].label = "院系（选填）"

    def clean_id_number(self):
        value = self.cleaned_data["id_number"].upper()
        if not re.fullmatch(r"[1-9][0-9]{16}[0-9X]", value):
            raise ValidationError("请填写18位身份证号，末位可为 X。")
        try:
            birthday = date(int(value[6:10]), int(value[10:12]), int(value[12:14]))
        except ValueError:
            raise ValidationError("身份证号中的出生日期无效。")
        from django.utils import timezone
        if birthday > timezone.localdate():
            raise ValidationError("身份证号中的出生日期不能晚于今天。")
        return value

STUDENT_PROFILE_FIELDS = ["username", "full_name", "id_number", "enrollment_status", "party_branch", "student_class", "political_status", "department"]


class StudentCreationForm(StudentProfileMixin, UserCreationForm):
    def save(self, commit=True):
        item = super().save(commit=False)
        item.password_change_required = True
        if commit:
            item.save()
        return item

    class Meta:
        model = User
        fields = STUDENT_PROFILE_FIELDS
        labels = {"username": "学号"}


class AccountForm(StudentProfileMixin, forms.ModelForm):
    class Meta:
        model = User
        fields = [*STUDENT_PROFILE_FIELDS, "is_active"]
        labels = {"username": "学号", "is_active": "允许登录"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].disabled = True

    def clean_is_active(self):
        value = self.cleaned_data["is_active"]
        if value and self.instance.is_archived:
            raise ValidationError("已归档学生须先恢复到信息列表，再允许登录。")
        return value


class CadreTenureDatesMixin:
    def clean_ends_on(self):
        return self.cleaned_data.get("ends_on") or date(2999, 12, 31)

    def clean(self):
        data = super().clean()
        if data.get("starts_on") and data.get("ends_on") and data["ends_on"] < data["starts_on"]:
            self.add_error("ends_on", "结束日期不能早于开始日期。")
        return data


class CadreTenureForm(CadreTenureDatesMixin, forms.ModelForm):
    class Meta:
        model = CadreTenure
        fields = ["position", "starts_on", "ends_on"]
        widgets = {name: forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}) for name in ["starts_on", "ends_on"]}
        help_texts = {"ends_on": "未填写时默认2999-12-31，可修改为实际结束日期。"}

class CadreTenureImportForm(CadreTenureDatesMixin, forms.Form):
    # 批量校验不逐行执行模型约束查询；共用日期校验，确认时仍由数据库约束兜底。
    position = forms.CharField(label="任职职务", max_length=100)
    starts_on = forms.DateField(label="开始日期")
    ends_on = forms.DateField(label="结束日期", required=False)


CadreTenureFormSet = forms.inlineformset_factory(User, CadreTenure, form=CadreTenureForm, extra=0,
    can_delete=True, max_num=100, validate_max=True, absolute_max=200)


class StudentFilterForm(forms.Form):
    q = forms.CharField(label="综合搜索", max_length=200, required=False,
        widget=forms.TextInput(attrs={"placeholder": "姓名、学号、班级、党支部或当前职务", "autocomplete": "off"}))
    student_class = forms.MultipleChoiceField(label="所在班级", required=False,
        widget=DropdownCheckboxSelect(label="所在班级", placeholder="不限班级"))
    party_branch = forms.MultipleChoiceField(label="所在党支部", required=False,
        widget=DropdownCheckboxSelect(label="所在党支部", placeholder="不限党支部"))
    political_status = forms.MultipleChoiceField(label="政治面貌", required=False,
        widget=DropdownCheckboxSelect(label="政治面貌", placeholder="不限政治面貌"),
        choices=[*User.Politics.choices, ("__empty__", "未填写")])
    enrollment_status = forms.ChoiceField(label="在校状态", required=False, choices=[("", "全部"), *User.EnrollmentStatus.choices])
    current_position = forms.MultipleChoiceField(label="当前履职职务", required=False,
        widget=DropdownCheckboxSelect(label="当前履职职务", placeholder="不限职务"))
    cadre = forms.ChoiceField(label="干部履职记录", required=False, choices=[("", "全部"), ("yes", "有履职记录"), ("no", "无履职记录")])
    is_active = forms.ChoiceField(label="登录状态", required=False, choices=[("", "全部"), ("1", "允许登录"), ("0", "已停用")])
    completeness = forms.ChoiceField(label="资料完整性", required=False, choices=[("", "全部"), ("complete", "已补全"), ("incomplete", "待补全")])
    archive = forms.ChoiceField(label="学生范围", choices=[("current", "当前学生"), ("archived", "已归档学生"), ("all", "全部学生")])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        students = User.objects.filter(is_staff=False, is_superuser=False)
        for name in ["student_class", "party_branch"]:
            values = students.exclude(**{name: ""}).order_by(name).values_list(name, flat=True).distinct()
            self.fields[name].choices = [(value, value) for value in values]
        self.fields["party_branch"].choices = [*self.fields["party_branch"].choices, ("__empty__", "未填写")]
        from .student_services import current_tenures
        titles = current_tenures().filter(student__is_staff=False, student__is_superuser=False).order_by("position").values_list("position", flat=True).distinct()
        self.fields["current_position"].choices = [(title, title) for title in titles]


class StudentDeleteConfirmForm(forms.Form):
    confirmed = forms.BooleanField(label="我已核对名单，确认按上述规则删除或归档这些学生。")


STUDENT_IMPORT_MAX_ROWS = 2000
STUDENT_IMPORT_MAX_BYTES = 5 * 1024 * 1024
STUDENT_IMPORT_HEADERS = ["学号", "姓名", "身份证号", "在校状态", "所在党支部", "所在班级", "政治面貌", "院系", "初始密码"]
STUDENT_IMPORT_COLUMNS = dict(zip(STUDENT_IMPORT_HEADERS[:-1], STUDENT_PROFILE_FIELDS))
STUDENT_POLITICS_UPDATE_HEADERS = ["学号", "政治面貌"]


class StudentImportRowForm(StudentCreationForm):
    """复用字段和密码校验；唯一性由整批查重及确认事务统一保证。"""

    def __init__(self, *args, import_mode="create", **kwargs):
        super().__init__(*args, **kwargs)
        if import_mode == "update":
            self.fields["password1"].required = self.fields["password2"].required = False

    def clean_username(self):
        return self.cleaned_data.get("username")

    def validate_unique(self):
        # 服务层已用一次查询检查全部现有账号，确认时仍会重新检查。
        pass


class StudentUpdateRowForm(StudentProfileMixin, forms.ModelForm):
    """只校验 CSV 中选定的字段，允许历史账号逐项补充资料。"""
    class Meta:
        model = User
        fields = STUDENT_PROFILE_FIELDS
        labels = {"username": "学号"}

    def __init__(self, *args, update_fields, **kwargs):
        super().__init__(*args, **kwargs)
        included = {"username", *update_fields}
        for name in list(self.fields):
            if name not in included:
                self.fields.pop(name)

    def validate_unique(self):
        # 学号只用于精确定位，权限和预览后的冲突由服务层重新检查。
        pass


class StudentImportForm(forms.Form):
    mode = forms.ChoiceField(label="导入方式", required=False, initial="create", choices=[("create", "新增学生账号及资料"),
        ("update", "补充 / 更新已有学生资料（保留密码、权限和登录状态）")])
    file = forms.FileField(
        label="学生信息 CSV",
        widget=forms.FileInput(attrs={"accept": ".csv"}),
        help_text=f"UTF-8 CSV，单次最多 {STUDENT_IMPORT_MAX_ROWS} 人、5 MB。新增须填写初始密码；更新可只保留学号和要修改的列，初始密码列须删除或留空。",
    )

    def clean_file(self):
        value = self.cleaned_data["file"]
        if Path(value.name).suffix.lower() != ".csv" or value.size > STUDENT_IMPORT_MAX_BYTES:
            raise ValidationError("请上传不超过 5 MB 的 CSV 文件。")
        return value

    def clean_mode(self):
        return self.cleaned_data.get("mode") or "create"


class StudentRosterUploadForm(forms.Form):
    file = forms.FileField(label="Excel名单", widget=forms.FileInput(attrs={"accept": ".xlsx,.xls"}),
        help_text="支持.xlsx和.xls，单次最多2000行、5 MB；学号列建议使用文本格式，姓名必须与账号一致。")

    def clean_file(self):
        value = self.cleaned_data["file"]
        if Path(value.name).suffix.lower() not in {".xlsx", ".xls"} or value.size > STUDENT_IMPORT_MAX_BYTES:
            raise ValidationError("请上传不超过5 MB的Excel文件（.xlsx或.xls）。")
        return value


class NotificationForm(forms.ModelForm):
    class Meta:
        model = Notification
        fields = ["title", "category", "summary", "body", "attachment"]
        labels = {"attachment": "通知附件"}
        widgets = {"body": forms.Textarea(attrs={"rows": 12}),
                   "attachment": forms.ClearableFileInput(attrs={"accept": ",".join(NOTIFICATION_FILE_TYPES)})}
        help_texts = {"attachment": "支持 Word、Excel、PDF、JPG/PNG/GIF/WebP/BMP 图片、PPT、TXT、CSV、ZIP，最多 20 MB。PDF 和图片可在线查看，其他格式下载后查看。"}

    def clean_attachment(self):
        value = self.cleaned_data.get("attachment")
        if isinstance(value, UploadedFile):
            validate_notification_upload(value)
        return value

    def clean(self):
        data = super().clean()
        if not data.get("body") and not data.get("attachment"):
            raise ValidationError("请填写通知正文或上传附件。")
        return data


class NotificationFilterForm(forms.Form):
    q = forms.CharField(label="搜索通知", max_length=200, required=False,
                        widget=forms.TextInput(attrs={"placeholder": "搜索通知标题、摘要或正文"}))
    category = forms.ChoiceField(label="通知类别", required=False,
                                  choices=[("", "全部类别"), *Notification.Category.choices])
    status = forms.ChoiceField(label="发布状态", required=False,
                               choices=[("", "全部状态"), *Notification.Status.choices])

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is None or not user.is_staff:
            self.fields.pop("status")


class PolicyForm(forms.ModelForm):
    category = forms.ChoiceField(label="制度类别", choices=[("", "请选择制度类别"), *PolicyRevision.Category.choices])
    class Meta:
        model = PolicyRevision
        fields = ["title", "category", "summary", "body", "attachment"]
        widgets = {"body": forms.Textarea(attrs={"rows": 10}), "attachment": forms.FileInput(attrs={"accept": ".pdf"})}

    def clean_attachment(self):
        return validate_upload(self.cleaned_data.get("attachment"), "pdf")

    def clean(self):
        data = super().clean()
        if not data.get("body") and not data.get("attachment"):
            raise ValidationError("请填写制度正文或上传 PDF 附件。")
        return data


class StudentIdentityForm(forms.ModelForm):
    account_name = forms.CharField(label="姓名", disabled=True, max_length=80, help_text="从登录账号带入；如有错误，请联系管理员更正账号资料。")
    account_number = forms.CharField(label="学号", disabled=True, max_length=150)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.update(account_name=user.full_name, account_number=user.username)
        self.order_fields(["account_name", "account_number", *self.fields])


class LeaveForm(StudentIdentityForm):
    province = forms.CharField(label="去往省份 / 直辖市", min_length=2, max_length=40, help_text="例如：浙江省、上海市。")
    city = forms.CharField(label="去往城市", min_length=2, max_length=40, help_text="例如：杭州市；直辖市请填写对应城市名。")
    tutor_signature = forms.ChoiceField(label="是否导师本人线下签字", choices=Leave._meta.get_field("tutor_signature").choices,
        widget=forms.RadioSelect)

    class Meta:
        model = Leave
        fields = ["kind", "reason", "starts_at", "ends_at", "province", "city", "contact", "tutor_signature"]
        widgets = {"starts_at": forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}),
                   "ends_at": forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}),
                   "reason": forms.Textarea(attrs={"rows": 5})}

    def clean(self):
        data = super().clean()
        if data.get("starts_at") and data.get("ends_at") and data["ends_at"] <= data["starts_at"]:
            self.add_error("ends_at", "结束时间必须晚于开始时间。")
        return data


class LeaveFilterForm(forms.Form):
    q = forms.CharField(label="关键词", max_length=200, required=False,
                        widget=forms.TextInput(attrs={"placeholder": "姓名、学号或请假原因"}))
    status = forms.ChoiceField(label="办理状态", required=False,
                               choices=[("", "全部状态"), *Leave.Status.choices])
    start_date = forms.DateField(label="开始日期", required=False, input_formats=["%Y-%m-%d"],
                                 widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
                                 error_messages={"invalid": "请输入有效的开始日期（YYYY-MM-DD）。"})
    end_date = forms.DateField(label="结束日期", required=False, input_formats=["%Y-%m-%d"],
                               widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
                               error_messages={"invalid": "请输入有效的结束日期（YYYY-MM-DD）。"})

    def clean(self):
        data = super().clean()
        if data.get("start_date") and data.get("end_date") and data["start_date"] > data["end_date"]:
            self.add_error("end_date", "结束日期不能早于开始日期。")
        return data


class MessageForm(forms.Form):
    body = forms.CharField(label="留言内容", max_length=5000, widget=forms.Textarea(attrs={"rows": 5}))


class ConversationForm(MessageForm):
    category = forms.ChoiceField(label="咨询类别", choices=[("", "请选择咨询类别"), *InquiryCategory.choices])
    subject = forms.CharField(label="咨询主题", max_length=200)
    field_order = ["category", "subject", "body"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["body"].label = "问题详情"
        self.fields["body"].widget.attrs["placeholder"] = "请完整说明问题背景、事情经过、已尝试的处理方式，以及希望解决的具体问题。"


class InquiryFilterForm(forms.Form):
    q = forms.CharField(label="搜索关键词", max_length=200, required=False,
                        widget=forms.TextInput(attrs={"placeholder": "输入关键词搜索"}))
    category = forms.ChoiceField(label="咨询类别", required=False,
                                  choices=[("", "全部类别"), *InquiryCategory.choices])


class FAQFilterForm(InquiryFilterForm):
    status = forms.ChoiceField(label="发布状态", required=False,
        choices=[("", "全部状态"), ("draft", "草稿"), ("published", "已发布"), ("withdrawn", "已撤销")])

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is None or not user.is_staff:
            self.fields.pop("status")


class ConsultationFilterForm(InquiryFilterForm):
    waiting = forms.ChoiceField(label="回复状态", required=False,
        choices=[("", "全部咨询"), ("1", "等待回复"), ("0", "已回复")])


class FAQForm(forms.ModelForm):
    class Meta:
        model = FAQ
        fields = ["question", "category", "answer"]
        widgets = {"answer": forms.Textarea(attrs={"rows": 10})}


class VideoMetadataForm(forms.ModelForm):
    category = forms.ChoiceField(label="视频类别", choices=[("", "请选择视频类别"), *Video.Category.choices])

    class Meta:
        model = Video
        fields = ["title", "category", "description"]
        widgets = {"description": forms.Textarea(attrs={"rows": 5})}


class VideoForm(VideoMetadataForm):
    class Meta(VideoMetadataForm.Meta):
        fields = ["title", "category", "description", "file"]
        widgets = {**VideoMetadataForm.Meta.widgets, "file": forms.FileInput(attrs={"accept": ".mp4,video/mp4"})}

    def clean_file(self):
        return validate_upload(self.cleaned_data.get("file"), "mp4")


class PolicyFilterForm(forms.Form):
    q = forms.CharField(label="搜索制度", required=False, max_length=200,
        widget=forms.TextInput(attrs={"placeholder": "搜索制度标题、摘要或正文"}))
    category = forms.ChoiceField(label="制度类别", required=False, choices=[("", "全部类别"), *PolicyRevision.Category.choices])


class VideoFilterForm(forms.Form):
    q = forms.CharField(label="搜索视频", required=False, max_length=200,
        widget=forms.TextInput(attrs={"placeholder": "搜索视频标题或内容介绍"}))
    category = forms.ChoiceField(label="视频类别", required=False, choices=[("", "全部类别"), *Video.Category.choices])


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={"accept": ".xls,.xlsx,.doc,.docx,.pdf"}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        if not data:
            return []
        values = data if isinstance(data, (list, tuple)) else [data]
        if len(values) > GUIDE_MAX_ATTACHMENTS:
            raise ValidationError(f"每份指南最多保留 {GUIDE_MAX_ATTACHMENTS} 个附件。")
        return [super(MultipleFileField, self).clean(value, initial) for value in values]


class RegistrationFilesField(forms.FileField):
    def clean(self, data, initial=None):
        values = data if isinstance(data, (tuple, list)) else ([data] if data else [])
        if not values:
            if self.required:
                raise ValidationError("请至少上传一份申请附件。")
            return []
        if len(values) > 10:
            raise ValidationError("每份申请最多上传 10 个附件。")
        return [super(RegistrationFilesField, self).clean(value, initial) for value in values]


class RegistrationForm(StudentIdentityForm):
    attachments = RegistrationFilesField(label="申请附件", validators=[validate_registration_upload],
        widget=MultipleFileInput(attrs={"accept": ",".join(REGISTRATION_FILE_TYPES)}),
        help_text='可去<a href="/guides/">办事指南（业务流程）模块</a>寻找申请模板。至少上传 1 份，最多 10 份；支持 JPG/PNG、Word、Excel、PDF，每份最多 20 MB。')
    has_dorm = forms.TypedChoiceField(label="是否有校内宿舍", choices=[("", "请选择"), ("yes", "是"), ("no", "否")],
        coerce=lambda value: value == "yes", empty_value=None, widget=forms.RadioSelect)

    class Meta:
        model = Registration
        fields = ["starts_at", "ends_at", "employer", "position", "address", "contact", "emergency_name", "emergency_contact",
                  "rental_reason", "reason_detail", "has_dorm", "dorm_number"]
        widgets = {"starts_at": forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}),
                   "ends_at": forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}),
                   "reason_detail": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *args, kind, source=None, **kwargs):
        self.kind = kind
        self.source = source
        super().__init__(*args, **kwargs)
        if source is not None:
            attachments = list(source.attachments.all())
            self.fields['retain_attachments'] = forms.MultipleChoiceField(label='保留原申请附件', required=False,
                choices=[(str(item.pk), item.original_name) for item in attachments], widget=forms.CheckboxSelectMultiple,
                initial=[str(item.pk) for item in attachments], help_text='勾选的附件会沿用到新申请；取消勾选仅影响本次重提，原申请附件仍保留。')
            self.fields['attachments'].required = False
            self.fields['attachments'].help_text = '可保留原附件或上传新附件；合计至少1份、最多10份，每份最多20 MB。'
        if kind == Registration.Kind.INTERNSHIP:
            for name in ["rental_reason", "reason_detail", "has_dorm", "dorm_number"]:
                self.fields.pop(name)
            self.fields["employer"].required = self.fields["position"].required = True
            self.fields["address"].label = "实习地址"
            self.fields["contact"].label = "个人联系方式"
            self.fields["attachments"].label = "实习申请附件"
        else:
            for name in ["employer", "position"]:
                self.fields.pop(name)
            self.fields["rental_reason"].required = True
            self.fields["reason_detail"].help_text = "租房原因选择“其他”时必填。"
            self.fields["address"].label = "租房详细地址（精确到户）"
            self.fields["address"].help_text = "填写省市区、街道、小区、楼栋及房间号。"
            self.fields["dorm_number"].help_text = "选择有校内宿舍时必填，例如：东区 3 栋 201。"
            self.fields["attachments"].label = "校外租房申请附件"
            self.order_fields(["account_name", "account_number", "rental_reason", "reason_detail", "starts_at", "ends_at",
                "address", "contact", "emergency_name", "emergency_contact", "has_dorm", "dorm_number", "attachments"])

    def clean(self):
        data = super().clean()
        if self.source is not None:
            retained = data.get('retain_attachments', [])
            uploaded = data.get('attachments', [])
            if not retained and not uploaded:
                self.add_error('attachments', '请保留或上传至少一份申请附件。')
            if len(retained)+len(uploaded) > 10:
                self.add_error('attachments', '保留与新增附件合计最多10份。')
        if data.get("starts_at") and data.get("ends_at") and data["ends_at"] <= data["starts_at"]:
            self.add_error("ends_at", "结束时间必须晚于开始时间。")
        if self.kind == Registration.Kind.RENTAL:
            if data.get("rental_reason") == "other" and not data.get("reason_detail"):
                self.add_error("reason_detail", "租房原因选择“其他”时，请说明具体理由。")
            if data.get("has_dorm") is True and not data.get("dorm_number"):
                self.add_error("dorm_number", "有校内宿舍时，请填写校内宿舍号。")
            if data.get("has_dorm") is False:
                data["dorm_number"] = ""
        return data


class RegistrationFilterForm(LeaveFilterForm):
    q = forms.CharField(label="关键词", max_length=200, required=False,
        widget=forms.TextInput(attrs={"placeholder": "姓名、学号、单位或地址"}))
    status = forms.ChoiceField(label="审核状态", required=False, choices=[("", "全部状态"), *Registration.Status.choices])


class RegistrationReviewForm(forms.Form):
    action = forms.ChoiceField(choices=[("approve", "通过"), ("reject", "驳回")])
    note = forms.CharField(label="审核意见", max_length=2000, required=False, widget=forms.Textarea(attrs={"rows": 4}))

    def clean(self):
        data = super().clean()
        if data.get("action") == "reject" and not data.get("note"):
            self.add_error("note", "驳回时必须填写驳回原因。")
        return data


class ServiceGuideForm(forms.ModelForm):
    attachments = MultipleFileField(label="上传附件", required=False, validators=[validate_guide_upload],
        help_text="可一次选择多个 Excel、Word 或 PDF 文件，每个最多 20 MB，每份指南最多 10 个附件。")
    remove_attachments = forms.ModelMultipleChoiceField(label="移除已有附件", required=False,
        queryset=GuideAttachment.objects.none(), widget=forms.CheckboxSelectMultiple)

    class Meta:
        model = ServiceGuide
        fields = ["title", "category", "owner", "contact", "office", "process"]
        widgets = {"process": forms.Textarea(attrs={"rows": 14})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.attachments.exists():
            self.fields["remove_attachments"].queryset = self.instance.attachments.all()
        else:
            self.fields.pop("remove_attachments")

    def clean(self):
        data = super().clean()
        existing = self.instance.attachments.count() if self.instance.pk else 0
        removed = len(data.get("remove_attachments", []))
        if existing - removed + len(data.get("attachments", [])) > GUIDE_MAX_ATTACHMENTS:
            raise ValidationError(f"每份指南最多保留 {GUIDE_MAX_ATTACHMENTS} 个附件，可先勾选移除旧附件。")
        return data


class ServiceGuideFilterForm(forms.Form):
    q = forms.CharField(label="搜索办事指南", max_length=200, required=False,
        widget=forms.TextInput(attrs={"placeholder": "搜索指南标题、负责人或办理流程"}))
    category = forms.ChoiceField(label="指南类别", required=False,
        choices=[("", "全部类别"), *ServiceGuide.Category.choices])
    status = forms.ChoiceField(label="发布状态", required=False,
        choices=[("", "全部状态"), *ServiceGuide.Status.choices])

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is None or not user.is_staff:
            self.fields.pop("status")


class AttendanceUploadForm(forms.Form):
    file = forms.FileField(label="月度打卡 CSV", widget=forms.FileInput(attrs={"accept": ".csv"}),
                          help_text="UTF-8 CSV，表头：学号、姓名、月份、打卡记录、请假天数。月份为 YYYY-MM，打卡记录填写当月打卡天数，两种天数均填写非负整数。单次最多 5000 行、2 MB，可包含多个月份。")

    def clean_file(self):
        value = self.cleaned_data["file"]
        if Path(value.name).suffix.lower() != ".csv" or value.size > 2 * 1024 * 1024:
            raise ValidationError("请上传不超过 2 MB 的 CSV 文件。")
        return value


ATTENDANCE_DAY_CHOICES = [("clock_days", "打卡天数"), ("leave_days", "请假天数"), ("total_days", "总天数")]
ATTENDANCE_SORT_CHOICES = [("student_number", "学号"), ("student_name", "姓名"), ("month", "月份"), *ATTENDANCE_DAY_CHOICES]


class AttendanceMonthForm(forms.Form):
    year = forms.IntegerField(label="年份", min_value=1900, max_value=9999)
    month = forms.ChoiceField(label="月份", required=False, choices=[("", "全年"), *[(str(i), f"{i} 月") for i in range(1, 13)]])


class AttendanceFilterForm(AttendanceMonthForm):
    month_from = forms.DateField(label="起始年月", required=False, input_formats=["%Y-%m"],
        widget=forms.DateInput(format="%Y-%m", attrs={"type": "month", "min": "1900-01", "max": "9999-12"}))
    month_to = forms.DateField(label="结束年月", required=False, input_formats=["%Y-%m"],
        widget=forms.DateInput(format="%Y-%m", attrs={"type": "month", "min": "1900-01", "max": "9999-12"}))
    q = forms.CharField(label="学生姓名 / 学号", required=False, max_length=200,
                        widget=forms.TextInput(attrs={"placeholder": "输入姓名或学号"}))
    day_kind = forms.ChoiceField(label="天数类别", required=False, choices=ATTENDANCE_DAY_CHOICES)
    days_min = forms.IntegerField(label="最少天数", required=False, min_value=0, max_value=31,
        widget=forms.NumberInput(attrs={"placeholder": "不限", "step": "1"}))
    days_max = forms.IntegerField(label="最多天数", required=False, min_value=0, max_value=31,
        widget=forms.NumberInput(attrs={"placeholder": "不限", "step": "1"}))
    sort = forms.ChoiceField(label="排序字段", choices=ATTENDANCE_SORT_CHOICES, widget=forms.HiddenInput)
    direction = forms.ChoiceField(label="排序方向", choices=[("asc", "升序"), ("desc", "降序")], widget=forms.HiddenInput)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        excluded = ["year", "month"] if user.is_staff else ["q", "month_from", "month_to", "day_kind", "days_min", "days_max"]
        for name in excluded:
            self.fields.pop(name)

    def clean(self):
        data = super().clean()
        for name in ["month_from", "month_to"]:
            if data.get(name) and data[name].year < 1900:
                self.add_error(name, "年月的年份不能早于1900年。")
        if data.get("month_from") and data.get("month_to") and data["month_to"] < data["month_from"]:
            self.add_error("month_to", "结束年月不能早于起始年月。")
        if data.get("days_min") is not None and data.get("days_max") is not None and data["days_max"] < data["days_min"]:
            self.add_error("days_max", "最多天数不能小于最少天数。")
        if "day_kind" in self.fields:
            data["day_kind"] = data.get("day_kind") or "clock_days"
        return data
