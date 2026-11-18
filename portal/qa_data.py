"""带独立账号分组的本地功能测试数据，不更改已有账号和业务。"""
import calendar
import csv
import io
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zipfile import ZipFile
from django.apps import apps
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Group
from django.core.management.base import CommandError
from django.db import models, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone
from .models import (AttendanceBatch, Audit, CadreTenure, Conversation, FAQ, GuideAttachment, InquiryCategory,
    Leave, LeaveEvent, Message, MonthlyAttendance, Notification, Policy, PolicyRevision, Publication,
    Registration, RegistrationAttachment, RegistrationEvent, ServiceGuide, StudentImportBatch,
    StudentRosterBatch, User, Video, StudentDailyActivity, ContentVisitDaily)
from .student_state import student_edit_digest, student_profile_digest

GROUP = "GradConnect QA dataset v1"
READY = "QA测试数据生成完成 v1"
PREFIX = "【测试】"
MEDIA_PREFIX = "qa_seed/v1/"
PASSWORD = "QaTest!2026"
ADMINS = ["qa_admin", "qa_admin2", "qa_root"]
LEGACY_TEST_NAMES = {
    15: '测试<同学&引号">',
    16: "测试长姓名" + "同" * 65,
    18: "",
    19: "=测试公式前缀",
}


def username(index):
    return "0099000012" if index == 12 else f"qa{index:06}"


def marker():
    return Audit.objects.filter(action=READY, actor__username="qa_admin").order_by("pk").first()


def _guard():
    if not settings.DEBUG:
        raise CommandError("测试数据命令仅允许在DJANGO_DEBUG=1的开发环境执行。")


def _create(model, rows, dates=None):
    if not rows:
        return rows
    model.objects.bulk_create(rows, batch_size=200)
    if dates:
        updated = any(field.name == "updated_at" for field in model._meta.fields)
        for row, moment in zip(rows, dates):
            row.created_at = moment
            if updated:
                row.updated_at = moment + timedelta(minutes=5)
        model.objects.bulk_update(rows, ["created_at", "updated_at"] if updated else ["created_at"], batch_size=200)
    return rows


def _csv(headers, rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def _advance_times(rows,children,dates,parent_id):
    by_id={row.pk:row for row in rows}
    for child,moment in zip(children,dates):
        parent=by_id[getattr(child,parent_id)]
        parent.updated_at=max(parent.updated_at,moment)
    if rows:
        type(rows[0]).objects.bulk_update(rows,["updated_at"],batch_size=200)


def _prepare_assets(students, as_of, created):
    folder = Path(settings.MEDIA_ROOT) / MEDIA_PREFIX
    folder.mkdir(parents=True, exist_ok=True)
    sources = Path(settings.BASE_DIR) / "portal" / "fixtures" / "qa_assets"
    if not (sources / "sample.mp4").exists():
        raise CommandError("缺少测试附件文件，请检查portal/fixtures/qa_assets是否完整。")
    for name in ["sample.pdf","sample.docx","sample.doc","sample.csv","sample.png","sample.jpg","sample.gif","sample.webp","sample.bmp","sample.txt","legacy.xls","graduate-list.xlsx","tenure-list.xlsx"]:
        if not (sources/name).exists():
            raise CommandError(f"测试附件不完整：{name}")
    for source in sorted(sources.iterdir()):
        if not source.is_file() or source.suffix not in {".pdf",".doc",".docx",".xls",".xlsx",".png",".jpg",".gif",".webp",".bmp",".txt",".csv",".mp4"}:
            continue
        target = folder / source.name
        if target.exists():
            if target.read_bytes() != source.read_bytes():
                raise CommandError(f"测试附件目录已有不同内容：{target.name}，未覆盖。")
        else:
            shutil.copyfile(source, target)
            created.append(target)
    files = {}
    from .forms import STUDENT_IMPORT_HEADERS
    base_month = as_of.strftime("%Y-%m")
    sample = [user for user in students if user.is_active and user.full_name][:12]
    files["valid/monthly-attendance.csv"] = _csv(["学号", "姓名", "月份", "打卡记录", "请假天数"],
        [[user.username, user.full_name, base_month, 20, 2] for user in sample])
    files["valid/update-politics.csv"] = _csv(["学号", "政治面貌"], [[user.username, "预备党员"] for user in sample])
    files["valid/new-students.csv"] = _csv(STUDENT_IMPORT_HEADERS,
        [[f"qa_new{i:04}", f"测试新增{i:04}", _identity(2000+i), "在校", "", "测试导入班", "群众", "测试学院", PASSWORD] for i in range(1, 51)])
    row = [sample[0].username, sample[0].full_name, base_month, 20, 2]
    headers = ["学号", "姓名", "月份", "打卡记录", "请假天数"]
    for label, changed in [("wrong-name", {1: "不匹配的测试姓名"}), ("negative-days", {3: -1}),
            ("half-day", {4: "1.5"}), ("too-many-days", {3: 31, 4: 1}), ("wrong-month", {2: "2026-13"})]:
        bad = row.copy()
        for index, value in changed.items():
            bad[index] = value
        files[f"invalid/{label}.csv"] = _csv(headers, [row, bad])
    files["invalid/duplicate-person-month.csv"] = _csv(headers, [row, row])
    files["invalid/disguised.pdf"] = b"This is deliberately not a PDF."
    files["invalid/unsupported.svg"] = b'<svg xmlns="http://www.w3.org/2000/svg"><text x="0" y="20">TEST</text></svg>'
    files["README.txt"] = ("GradConnect功能测试文件包\n所有资料均为虚构。\n"
        "valid/new-students.csv：新增50个qa_new账号，学生账号初始密码QaTest!2026。\n"
        "valid/update-politics.csv：只更新政治面貌；其余字段应保持不变。\n"
        "valid/monthly-attendance.csv：同人同月重导应更新，不能重复创建。\n"
        "graduate-list.xlsx：批量设置毕业；tenure-list.xlsx：导入履职记录。\n"
        "invalid目录：应在上传校验时整批拒绝。请核对无部分写入。\n"
        "新增账号通过页面导入后不自动属于初始测试分组，清理命令不会擅自删除这些额外账号。\n").encode("utf-8")
    bundle = folder / "fixture-bundle.zip"
    if not bundle.exists():
        created.append(bundle)
        with ZipFile(bundle, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
            for name in ["graduate-list.xlsx", "tenure-list.xlsx"]:
                archive.write(folder / name, name)
    return folder


def _identity(index):
    born = date(1990, 1, 1) + timedelta(days=index % 5000)
    base = "990000" + born.strftime("%Y%m%d") + f"{index % 1000:03}"
    checksum = "10X98765432"[sum(int(digit)*weight for digit, weight in zip(base, [7,9,10,5,8,4,2,1,6,3,7,9,10,5,8,4,2])) % 11]
    return base + checksum


def seed_dataset(count=1500, as_of=None, progress=lambda value: None):
    _guard()
    if not 60 <= count <= 5000:
        raise CommandError("学生数量须在60到5000之间，默认1500。")
    if marker():
        return dataset_report(), False
    names = ADMINS + [username(index) for index in range(1, count+1)]
    expected = {name.casefold() for name in names}
    if Group.objects.filter(name=GROUP).exists() or any(name.casefold() in expected for name in User.objects.values_list("username", flat=True)):
        raise CommandError("测试账号或分组已存在但没有完整生成标记；未覆盖任何账号，请先核对。")
    as_of = as_of or timezone.localdate()
    anchor = timezone.make_aware(datetime.combine(as_of, time(12)))
    users = []
    politics = ["", *User.Politics.values]
    for index in range(1, count+1):
        user = User(username=username(index), full_name=f"测试学生{index:04}", department=f"测试学院{index%4+1}",
            id_number=_identity(index), student_class=f"测试{2024+index%3}级{index%12+1:02}班",
            party_branch=f"测试第{index%6+1}党支部" if index%3 == 0 else "",
            political_status=politics[index%len(politics)], enrollment_status="graduated" if index%7 == 0 or index == 3 else "enrolled",
            is_archived=index%31 == 5, is_active=index%29 != 4 and index%31 != 5)
        if index%23 == 6:
            user.id_number, user.student_class = "", ""
        if index == 17:
            user.student_class = '测试班级,"CSV引号"'
        users.append(user)
    admins = [User(username=name, full_name=f"测试管理员{i+1}", is_staff=True) for i, name in enumerate(ADMINS)]
    progress(f"准备{count}名学生和3名管理员的独立密码哈希…")
    with ThreadPoolExecutor(max_workers=2) as pool:
        for index, (user, encoded) in enumerate(zip([*admins, *users], pool.map(make_password, [PASSWORD]*(count+3))), 1):
            user.password = encoded
            if index%100 == 0 or index == count+3:
                progress(f"账号密码准备：{index}/{count+3}")
    copied = []
    try:
        with transaction.atomic():
            if User.objects.filter(username__in=names).exists() or Group.objects.filter(name=GROUP).exists():
                raise CommandError("准备期间测试账号被创建，本次未写入。")
            group = Group.objects.create(name=GROUP)
            User.objects.bulk_create([*admins, *users], batch_size=200)
            User.groups.through.objects.bulk_create([User.groups.through(user_id=user.pk, group_id=group.pk) for user in [*admins, *users]], batch_size=200)
            main, other, root = admins
            _prepare_assets(users, as_of, copied)
            asset = lambda name: MEDIA_PREFIX + name
            # 末20名保留为无任何关联的删除样本，其余均有历史数据。
            connected = users[:-20]
            progress("生成履职、通知、指南、制度及学习资源…")
            tenures = []
            def role(user, position, start, end=date(2999,12,31)):
                tenures.append(CadreTenure(student=user, position=position, starts_on=start, ends_on=end))
            for index, user in enumerate(connected, 1):
                if index == 13:
                    for number in range(98):
                        role(user, f"测试历史职务{number+1:03}", as_of-timedelta(days=1000+number*3), as_of-timedelta(days=999+number*3))
                    role(user, "测试班长", as_of-timedelta(days=90))
                    role(user, "测试团支书", as_of-timedelta(days=30))
                elif index == 7:
                    role(user, "测试历史班长", as_of-timedelta(days=100), as_of-timedelta(days=1))
                elif index == 8:
                    role(user, "测试未来班长", as_of+timedelta(days=1))
                elif index == 9:
                    role(user, "测试今日开始", as_of)
                elif index == 10:
                    role(user, "测试今日结束", as_of-timedelta(days=30), as_of)
                elif index == 1 or index%3 == 0:
                    role(user, "测试班长", as_of-timedelta(days=100))
                    role(user, "测试学习委员", as_of-timedelta(days=20))
                    role(user, "测试往届委员", as_of-timedelta(days=400), as_of-timedelta(days=200))
                    role(user, "测试下届委员", as_of+timedelta(days=30))
            _create(CadreTenure, tenures)
            notices, notice_dates = [], []
            formats = [None,"sample.pdf","sample.docx","graduate-list.xlsx","sample.png","sample.jpg","sample.gif","sample.webp","sample.bmp","sample.txt","sample.csv","fixture-bundle.zip","legacy.xls","sample.doc"]
            for category, label in Notification.Category.choices:
                for index in range(18):
                    filename = formats[index%len(formats)]
                    notices.append(Notification(title=f"{PREFIX}{label}通知{index+1:02}", category=category,
                        summary="虚构通知，用于分类、关键词、附件和分页测试。", body=(f"{PREFIX}{label}事项说明\n请按测试流程核对材料。\n联系人：测试老师；邮箱qa@example.invalid。" if index%4 else ""),
                        attachment=asset(filename) if filename else "", status="withdrawn" if index>=14 else "published", created_by=main, updated_by=other,
                        published_at=anchor-timedelta(days=index*3)))
                    if not notices[-1].body and not filename:
                        notices[-1].body=PREFIX+"纯正文通知，正文与附件至少保留一项。"
                    notice_dates.append(anchor-timedelta(days=index*3))
            _create(Notification, notices, notice_dates)
            guides, guide_dates = [], []
            for category, label in ServiceGuide.Category.choices:
                for index in range(16):
                    guides.append(ServiceGuide(title=f"{PREFIX}{label}办事流程{index+1:02}", category=category, owner=f"测试负责人{index+1}",
                        contact="00000000000 / qa@example.invalid", office=f"虚构测试楼{index+1}室（非真实地址）",
                        process=f"{PREFIX}{label}办理步骤\n1. 下载表单并核对资料。\n2. 上传申请材料并提交。\n3. 等待管理员审核，按回复办理。\n\n本内容仅用于功能测试。",
                        status="withdrawn" if index>=14 else "published", created_by=main, updated_by=other,
                        published_at=anchor-timedelta(days=index)))
                    guide_dates.append(anchor-timedelta(days=index))
            _create(ServiceGuide, guides, guide_dates)
            attachments=[]
            for index, guide in enumerate(guides):
                for name in (["sample.pdf","sample.docx","graduate-list.xlsx","tenure-list.xlsx","legacy.xls"] if index%3 == 0 else ["sample.pdf"]):
                    attachments.append(GuideAttachment(guide=guide, file=asset(name), original_name="测试申请材料-"+name))
            _create(GuideAttachment, attachments)
            policies=[]
            for category, label in PolicyRevision.Category.choices:
                for index in range(6):
                    policy=Policy.objects.create(created_by=main, status=["published","published","published","draft","withdrawn","published"][index])
                    old=PolicyRevision.objects.create(policy=policy, number=1, title=f"{PREFIX}{label}制度{index+1:02}", category=category,
                        summary="虚构制度，用于发布版本、草稿隔离和附件访问测试。", body=f"{PREFIX}{label}制度正文\n适用范围、材料要求、办理程序均为测试内容。",
                        attachment=asset("sample.pdf") if index%2 == 0 else "", is_draft=index == 3)
                    if index != 3:
                        policy.published_revision=old
                        policy.save(update_fields=["published_revision"])
                    if index in [0,1,5]:
                        PolicyRevision.objects.create(policy=policy, number=2, title=f"{PREFIX}{label}制度{index+1:02}待发布修改", category="other" if category != "other" else "daily",
                            body=PREFIX+"未发布草稿口令QA-DRAFT-ONLY，学生不应看到。", attachment=asset("sample.pdf"), is_draft=True)
                    policies.append(policy)
            videos=[]
            for category, label in Video.Category.choices:
                for index in range(6):
                    videos.append(Video(title=f"{PREFIX}{label}学习视频{index+1:02}", category=category,
                        description="虚构测试视频，6秒彩色画面与轻声提示音，用于播放、拖动和Range下载验证。", file=asset("sample.mp4"),
                        is_deleted=index == 5, uploaded_by=main))
            _create(Video, videos, [anchor-timedelta(days=index) for index in range(len(videos))])
            progress("生成请销假全状态、实习与租房审核记录…")
            leaves, leave_dates=[],[]
            leave_count=max(300,count)
            for index in range(leave_count+10):
                user=connected[index%len(connected)] if index < leave_count else users[(index-leave_count)//5]
                status=Leave.Status.values[index%5]
                start=anchor+timedelta(days=index%40-10) if status in {"pending","approved"} else anchor-timedelta(days=index%365+2)
                if index%17 == 0:
                    start=timezone.make_aware(datetime.combine(as_of.replace(day=1),time.min))-timedelta(minutes=30)
                if index == 18:
                    start=timezone.make_aware(datetime(2024,2,29,23,30))
                if index == 19:
                    start=timezone.make_aware(datetime(as_of.year,1,1,0,0))-timedelta(minutes=30)
                end=start+timedelta(days=index%4+1,hours=2)
                moment=min(start-timedelta(days=2),anchor-timedelta(hours=2))
                leaves.append(Leave(student=user, student_name=user.full_name, student_number=user.username, kind=Leave.Kind.values[index%3],
                    starts_at=start, ends_at=end, reason=f"{PREFIX}请假样本{index+1:04}，含逗号,引号\"及换行\n用于筛选和导出测试。", province=["浙江省","江苏省","北京市","广东省"][index%4],
                    city=["杭州市","南京市","北京市","广州市"][index%4], destination=f"测试地址{index%9+1}号（虚构）",contact="00000000000", tutor_signature="yes" if index%2 else "no",status=status))
                leave_dates.append(moment)
            _create(Leave, leaves, leave_dates)
            events,event_dates=[],[]
            for index,item in enumerate(leaves):
                moment=leave_dates[index]
                events.append(LeaveEvent(leave=item, actor=item.student, status="pending", note=PREFIX+"提交完整请假申请"));event_dates.append(moment)
                path=["approved","closed"] if item.status == "closed" else [] if item.status == "pending" else [item.status]
                for step,status in enumerate(path,1):
                    actor=item.student if status == "withdrawn" else main if step%2 else other
                    note={"approved":"审核通过，材料齐全","rejected":"驳回：测试材料缺少导师签字，请补充","withdrawn":"申请人取消待审核请假","closed":"已在线下确认返校，完成销假"}[status]
                    events.append(LeaveEvent(leave=item,actor=actor,status=status,note=PREFIX+note));event_dates.append(moment+timedelta(hours=step))
            _create(LeaveEvent,events,event_dates)
            _advance_times(leaves,events,event_dates,"leave_id")
            # 驳回后重提仍关联原申请，便于检查历史跳转。
            for item in [item for item in leaves if item.status == "rejected"][:12]:
                again=Leave.objects.create(student=item.student, student_name=item.student_name, student_number=item.student_number,
                    kind=item.kind, starts_at=anchor+timedelta(days=2), ends_at=anchor+timedelta(days=3), reason=PREFIX+"补齐材料后重新提交",
                    province=item.province,city=item.city,destination=item.destination,contact=item.contact,tutor_signature="yes",previous=item)
                LeaveEvent.objects.create(leave=again,actor=item.student,status="pending",note=PREFIX+"驳回后重新提交")
            registrations,registration_dates=[],[]
            for kind in Registration.Kind.values:
                registration_count=max(60,count//3)
                for index in range(registration_count+2*len(Registration.Status.values)):
                    user=connected[index%len(connected)] if index < registration_count else users[(index-registration_count)//len(Registration.Status.values)]
                    start=anchor+timedelta(days=index%70-35)
                    registrations.append(Registration(student=user,student_name=user.full_name or user.username,student_number=user.username,kind=kind,
                        starts_at=start,ends_at=start+timedelta(days=30+index%100),status=Registration.Status.values[index%len(Registration.Status.values)],
                        employer=f"测试实习单位{index%15+1}" if kind == "internship" else "", position=["测试研发岗","测试行政岗","测试助理岗"][index%3] if kind == "internship" else "",
                        address=f"测试省测试市测试路0号{index%100+1}室（虚构到户地址）",contact="00000000000",emergency_name="测试紧急联系人",emergency_contact="00000000000",
                        rental_reason=Registration.RentalReason.values[index%3] if kind == "rental" else "",reason_detail=PREFIX+"其他原因：演练租房申请说明" if kind == "rental" and index%3 == 2 else "",
                        has_dorm=bool(index%2) if kind == "rental" else None,dorm_number=f"测试宿舍{index%12+1}-{index%100+1}" if kind == "rental" and index%2 else ""))
                    registration_dates.append(anchor-timedelta(days=index%120+1))
            _create(Registration,registrations,registration_dates)
            reg_files,reg_events,reg_event_dates=[],[],[]
            for index,item in enumerate(registrations):
                names=["sample.pdf","sample.docx","sample.png","graduate-list.xlsx"]
                name=names[index%len(names)]
                reg_files.append(RegistrationAttachment(registration=item,file=asset(name),original_name="测试申请-"+name))
                reg_events.append(RegistrationEvent(registration=item,actor=item.student,status="pending",note=PREFIX+"提交完整登记与附件"));reg_event_dates.append(registration_dates[index])
                if item.status != "pending":
                    reg_events.append(RegistrationEvent(registration=item,actor=item.student if item.status == 'withdrawn' else other,
                        status=item.status,note=PREFIX+('学生撤回申请，待修改重提' if item.status == 'withdrawn' else "审核通过" if item.status == "approved" else "驳回：附件材料不完整，请补充后重新登记")))
                    reg_event_dates.append(registration_dates[index]+timedelta(hours=1))
            _create(RegistrationAttachment,reg_files);_create(RegistrationEvent,reg_events,reg_event_dates)
            _advance_times(registrations,reg_events,reg_event_dates,"registration_id")
            progress("生成分类咨询、公开问答和跨年月度打卡…")
            conversations,conv_dates=[],[]
            for category,label in InquiryCategory.choices:
                for index in range(20):
                    user=connected[(index//4)%len(connected)]
                    conversations.append(Conversation(student=user,category=category,subject=f"{PREFIX}{label}私人咨询{index+1:02}",awaiting_reply=index%4 in {0,3}))
                    conv_dates.append(anchor-timedelta(days=index*2,hours=1))
            _create(Conversation,conversations,conv_dates)
            messages,message_dates=[],[]
            for index,item in enumerate(conversations):
                mode=index%20%4
                body=f"{PREFIX}{item.student.username}的私人内容，隐私测试口令QA-PRIVATE-{item.pk}，其他学生不应看到。"
                messages.append(Message(conversation=item,author=item.student,body=body));message_dates.append(conv_dates[index])
                if mode != 0:
                    messages.append(Message(conversation=item,author=main,body=PREFIX+"管理员一对一回复：请核对办理流程并准备材料。"));message_dates.append(conv_dates[index]+timedelta(minutes=10))
                if mode in {2,3}:
                    messages.append(Message(conversation=item,author=item.student,body=PREFIX+"学生继续追问，测试多轮对话。"));message_dates.append(conv_dates[index]+timedelta(minutes=20))
                if mode == 2:
                    messages.append(Message(conversation=item,author=other,body=PREFIX+"另一管理员补充回复，问题已处理。"));message_dates.append(conv_dates[index]+timedelta(minutes=30))
            _create(Message,messages,message_dates)
            _advance_times(conversations,messages,message_dates,"conversation_id")
            faqs=[]
            for category,label in InquiryCategory.choices:
                for index in range(24):
                    faqs.append(FAQ(question=f"{PREFIX}{label}官方问题{index+1:02}如何办理？",answer=PREFIX+"公开解答：查询办事指南，核对材料，再提交申请。此处不包含原始私人咨询的身份和隐私口令。",
                        category=category,status=["published","published","published","draft","withdrawn"][index%5],created_by=main,
                        source=next(item for item in conversations if item.category == category and not item.awaiting_reply) if index%4 == 0 else None))
            _create(FAQ,faqs,[anchor-timedelta(days=index%30) for index in range(len(faqs))])
            month_number=as_of.year*12+as_of.month-1
            months=sorted({date((month_number-offset)//12,(month_number-offset)%12+1,1) for offset in range(24)} | {date(2024,2,1)})
            batch=AttendanceBatch.objects.create(uploaded_by=main,row_count=0,is_confirmed=True)
            attendance=[]
            for index,user in enumerate(connected,1):
                if not user.full_name:
                    continue
                for offset,month in enumerate(months):
                    if index%37 == 0 and offset%3 == 0:
                        continue
                    length=calendar.monthrange(month.year,month.month)[1]
                    if index == 25:
                        clock,leave=0,0
                    elif index == 26:
                        clock,leave=length,0
                    elif index == 27:
                        clock,leave=0,length
                    else:
                        leave=(index+offset)%6
                        clock=(index*7+offset*3)%(length-leave+1)
                    attendance.append(MonthlyAttendance(student=user,student_number=user.username,student_name=user.full_name,month=month,clock_days=clock,leave_days=leave,batch=batch))
            _create(MonthlyAttendance,attendance)
            batch.row_count=len(attendance);batch.save(update_fields=["row_count"])
            # 可直接进入的已确认、待确认、过期及旧版预览。
            user=users[0]
            profile={field:getattr(user,field) for field in ["full_name","id_number","enrollment_status","party_branch","student_class","political_status","department"]}
            payload={"username":user.username,"student_id":user.pk,"source_digest":student_profile_digest(user),**profile,"political_status":"party","update_fields":["political_status"]}
            StudentImportBatch.objects.create(uploaded_by=main,mode="update",row_count=1,payload=[payload])
            expired=StudentImportBatch.objects.create(uploaded_by=other,mode="update",row_count=1,payload=[payload])
            StudentImportBatch.objects.filter(pk=expired.pk).update(created_at=anchor-timedelta(days=2))
            StudentImportBatch.objects.create(uploaded_by=main,row_count=1,payload=[{"username":"old-preview"}],format_version=1)
            for kind in StudentRosterBatch.Kind.values:
                row={"username":user.username,"student_id":user.pk,"name":user.full_name,"source_digest":student_profile_digest(user) if kind == "graduate" else student_edit_digest(user),"original_status":user.enrollment_status,
                     "action":"在校 → 毕业" if kind == "graduate" else "新增履职记录"}
                if kind == "tenure":
                    row.update(position="测试待导入职务",starts_on=as_of.isoformat(),ends_on="2999-12-31",tenure_id=None)
                StudentRosterBatch.objects.create(kind=kind,uploaded_by=main,row_count=1,payload=[row])
            record=next(item for item in attendance if item.student.is_active and item.month == as_of.replace(day=1))
            row={"student_id":record.student_id,"username":record.student_number,"name":record.student_name,"month":record.month.strftime("%Y-%m"),"clock_days":20,"leave_days":2,"total_days":22}
            AttendanceBatch.objects.create(uploaded_by=main,row_count=1,payload=[row])
            expired=AttendanceBatch.objects.create(uploaded_by=other,row_count=1,payload=[row])
            AttendanceBatch.objects.filter(pk=expired.pk).update(created_at=anchor-timedelta(days=2))
            AttendanceBatch.objects.create(uploaded_by=main,row_count=1,payload=[{"date":"2026-11-01"}],format_version=1)
            audits=[Audit(actor=main if index%2 else other,action=PREFIX+"业务操作演练",target=f"portal.Leave:{item.pk}") for index,item in enumerate(leaves)]
            _create(Audit,audits,[anchor-timedelta(minutes=index) for index in range(len(audits))])
            Audit.objects.create(actor=main,action=READY,target=json.dumps({"version":1,"as_of":as_of.isoformat(),"students":count},ensure_ascii=False))
    except BaseException:
        for file in reversed(copied):
            file.unlink(missing_ok=True)
        raise
    return dataset_report(), True


def repair_test_names():
    """Normalize only known legacy QA names, preserving user edits and other history."""
    _guard()
    counts = {"users": 0, "leaves": 0, "registrations": 0, "monthly_attendance": 0}
    if not marker():
        return counts
    with transaction.atomic():
        for index, legacy_name in LEGACY_TEST_NAMES.items():
            user = User.objects.select_for_update().filter(groups__name=GROUP, username=username(index),
                is_staff=False, is_superuser=False).first()
            if user is None:
                continue
            if user.full_name == legacy_name:
                user.full_name = f"测试学生{index:04}"
                counts["users"] += User.objects.filter(pk=user.pk, full_name=legacy_name).update(full_name=user.full_name)
            # Snapshot fields are deliberately repaired only for these QA identities
            # and exact legacy values. Manually changed names remain untouched.
            if not user.full_name or user.full_name == legacy_name:
                continue
            for key, model in [("leaves", Leave), ("registrations", Registration), ("monthly_attendance", MonthlyAttendance)]:
                old_names = [legacy_name]
                if not legacy_name and model is Registration:
                    old_names.append(user.username)
                counts[key] += model.objects.filter(student=user, student_name__in=old_names).update(student_name=user.full_name)
    return counts


def dataset_report():
    ready=marker()
    if not ready:
        return {"exists":False}
    users=User.objects.filter(groups__name=GROUP)
    ids=list(users.values_list("pk",flat=True))
    admins=list(users.filter(is_staff=True).values_list("pk",flat=True))
    stats={"students":users.filter(is_staff=False).count(),"admins":len(admins),"cadre_tenures":CadreTenure.objects.filter(student_id__in=ids).count(),
        "notifications":Notification.objects.filter(created_by_id__in=admins,title__startswith=PREFIX).count(),
        "guides":ServiceGuide.objects.filter(created_by_id__in=admins,title__startswith=PREFIX).count(),
        "policies":Policy.objects.filter(created_by_id__in=admins).count(),"policy_revisions":PolicyRevision.objects.filter(policy__created_by_id__in=admins).count(),
        "videos":Video.objects.filter(uploaded_by_id__in=admins).count(),"leaves":Leave.objects.filter(student_id__in=ids).count(),
        "internships":Registration.objects.filter(student_id__in=ids,kind="internship").count(),"rentals":Registration.objects.filter(student_id__in=ids,kind="rental").count(),
        "conversations":Conversation.objects.filter(student_id__in=ids).count(),"messages":Message.objects.filter(conversation__student_id__in=ids).count(),
        "faqs":FAQ.objects.filter(created_by_id__in=admins,question__startswith=PREFIX).count(),"monthly_attendance":MonthlyAttendance.objects.filter(student_id__in=ids).count()}
    return {"exists":True,**json.loads(ready.target),"counts":stats,"media_prefix":MEDIA_PREFIX}


def clear_dataset():
    _guard()
    ready=marker()
    if not ready:
        return False
    metadata=json.loads(ready.target)
    users=User.objects.filter(groups__name=GROUP)
    expected=set(ADMINS+[username(index) for index in range(1,metadata["students"]+1)])
    if any(name not in expected for name in users.values_list("username",flat=True)):
        raise CommandError("测试分组包含未登记账号，本次未清理，请先核对分组成员。")
    ids=list(users.values_list("pk",flat=True));admins=list(users.filter(is_staff=True).values_list("pk",flat=True))
    if MonthlyAttendance.objects.filter(batch__uploaded_by_id__in=admins).exclude(student_id__in=ids).exists():
        raise CommandError("测试管理员的打卡批次关联了非测试学生，本次未清理。")
    conversations=Conversation.objects.filter(student_id__in=ids)
    if FAQ.objects.filter(source__in=conversations).exclude(created_by_id__in=admins,question__startswith=PREFIX).exists():
        raise CommandError("测试咨询关联了非测试官方问答，本次未清理。")
    if Leave.objects.filter(previous__student_id__in=ids).exclude(student_id__in=ids).exists():
        raise CommandError("测试请假关联了非测试申请，本次未清理。")
    try:
        with transaction.atomic():
            FAQ.objects.filter(created_by_id__in=admins,question__startswith=PREFIX).delete()
            Conversation.objects.filter(student_id__in=ids).delete()
            Leave.objects.filter(student_id__in=ids).delete()
            Registration.objects.filter(student_id__in=ids).delete()
            CadreTenure.objects.filter(student_id__in=ids).delete()
            MonthlyAttendance.objects.filter(student_id__in=ids).delete()
            AttendanceBatch.objects.filter(uploaded_by_id__in=admins).delete()
            StudentImportBatch.objects.filter(uploaded_by_id__in=admins).delete()
            StudentRosterBatch.objects.filter(uploaded_by_id__in=admins).delete()
            Notification.objects.filter(created_by_id__in=admins,title__startswith=PREFIX).delete()
            ServiceGuide.objects.filter(created_by_id__in=admins,title__startswith=PREFIX).delete()
            Policy.objects.filter(created_by_id__in=admins).delete()
            Video.objects.filter(uploaded_by_id__in=admins).delete()
            Audit.objects.filter(actor_id__in=ids).delete()
            ContentVisitDaily.objects.filter(student_id__in=ids).delete()
            StudentDailyActivity.objects.filter(student_id__in=ids).delete()
            users.delete()
            Group.objects.filter(name=GROUP).delete()
    except ProtectedError:
        raise CommandError("测试账号仍关联非本套内容，已回滚全部清理，请先核对关联记录。")
    # 只移除专属目录，正常上传文件和仍有引用的测试附件不删除。
    referenced=False
    for model in apps.get_app_config("portal").get_models():
        for field in model._meta.fields:
            if isinstance(field,models.FileField) and model.objects.filter(**{field.name+"__startswith":MEDIA_PREFIX}).exists():
                referenced=True
    if not referenced:
        shutil.rmtree(Path(settings.MEDIA_ROOT)/MEDIA_PREFIX,ignore_errors=True)
    return True
