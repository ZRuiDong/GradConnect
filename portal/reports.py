"""Detailed Excel snapshots, sharing the list filters and keeping memory bounded."""
import math
import unicodedata
from tempfile import SpooledTemporaryFile
from django.core.exceptions import ValidationError
from django.db.models import Count, Prefetch, Q, Sum
from django.http import FileResponse
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from .models import Audit, CadreTenure, ContentVisitDaily, StudentDailyActivity


MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
ROW_LIMIT = 1048570


def local_datetime(value):
    return timezone.localtime(value).replace(tzinfo=None) if value else None


class Report:
    def __init__(self, title, scope):
        self.book = Workbook(write_only=True)
        self.title, self.scope, self.created = title, scope, timezone.localtime()
        self.body_font = Font(name='等线', size=11, color='334155')
        self.title_font = Font(name='等线', size=17, bold=True, color='203D70')
        self.header_font = Font(name='等线', size=11, bold=True, color='FFFFFF')
        self.alignment = Alignment(vertical='center', wrap_text=True)
        self.header_fill = PatternFill('solid', fgColor='4265BA')
        self.text_heights = {}

    def sheet(self, name, columns):
        sheet = self.book.create_sheet(name)
        sheet.report_columns = columns
        sheet.report_row_count = 0
        sheet.sheet_view.showGridLines = False
        sheet.freeze_panes = 'A6'
        sheet.print_title_rows = '1:5'
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.orientation = 'landscape'
        sheet.page_setup.paperSize = Worksheet.PAPERSIZE_A3
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        for index, (_, width) in enumerate(columns, 1):
            sheet.column_dimensions[get_column_letter(index)].width = width
        self.row(sheet, [self.title+' · '+name], title=True)
        self.row(sheet, ['导出范围', self.scope])
        self.row(sheet, ['导出时间（北京时间）', self.created.replace(tzinfo=None)])
        self.row(sheet, [])
        self.row(sheet, [label for label, _ in columns], header=True)
        return sheet

    def row(self, sheet, values, header=False, title=False):
        cells = []
        height = 34 if header or title else 24
        for index, value in enumerate(values):
            if isinstance(value, str):
                value = ILLEGAL_CHARACTERS_RE.sub('', value)
            cell = WriteOnlyCell(sheet, value=value)
            if isinstance(value, str):
                # Preserve leading zeroes/18-digit IDs and prevent formula injection.
                cell.data_type = 's'
                cell.number_format = '@'
            elif hasattr(value, 'hour'):
                cell.number_format = 'yyyy-mm-dd hh:mm'
            elif hasattr(value, 'year'):
                label = sheet.report_columns[index][0] if index < len(sheet.report_columns) else ''
                cell.number_format = 'yyyy-mm' if label == '月份' else 'yyyy-mm-dd'
            elif isinstance(value, (int, float)):
                cell.number_format = '#,##0.0' if isinstance(value, float) else '#,##0'
            cell.font = self.header_font if header else self.title_font if title else self.body_font
            cell.alignment = self.alignment
            if header:
                cell.fill = self.header_fill
            if isinstance(value, str) and not title and index < len(sheet.report_columns):
                width = sheet.report_columns[index][1]
                key = (value, width)
                text_height = self.text_heights.get(key)
                if text_height is None:
                    units = sum(2 if unicodedata.east_asian_width(char) in {'W', 'F'} else 1 for char in value)
                    text_height = min(300, math.ceil(units/max(1, width-2))*15+8)
                    if len(self.text_heights) >= 8192:
                        self.text_heights.clear()
                    self.text_heights[key] = text_height
                height = max(height, text_height)
            cells.append(cell)
        sheet.report_row_count += 1
        sheet.row_dimensions[sheet.report_row_count].height = height
        sheet.append(cells)

    def finish_sheet(self, sheet):
        columns = len(sheet.column_dimensions)
        sheet.auto_filter.ref = f'A5:{get_column_letter(columns)}{max(5, sheet.report_row_count)}'

    def response(self, actor, count):
        output = SpooledTemporaryFile(max_size=4*1024*1024, mode='w+b')
        try:
            self.book.save(output)
            output.seek(0)
            Audit.objects.create(actor=actor, action='导出'+self.title,
                target=f'{count}条；{self.scope}'[:200])
            filename = self.title+'-'+self.created.strftime('%Y%m%d-%H%M%S')+'.xlsx'
            response = FileResponse(output, as_attachment=True, filename=filename, content_type=MIME)
            response['Cache-Control'] = 'private, no-store'
            return response
        except BaseException:
            output.close()
            raise


def ensure_size(count):
    if count > ROW_LIMIT:
        raise ValidationError('记录超过Excel单页上限，请缩小筛选范围后重新导出。')


def account_state(student):
    return '已归档并停用' if student.is_archived else '正常' if student.is_active else '已停用'


def filter_scope(form):
    parts = []
    for name, field in form.fields.items():
        value = form.cleaned_data.get(name)
        if value in (None, '', [], ()) or name in {'sort', 'direction'}:
            continue
        choices = dict(getattr(field, 'choices', ()))
        values = value if isinstance(value, (list, tuple)) else [value]
        parts.append(str(field.label)+'：'+'、'.join(str(choices.get(item, item)) for item in values))
    return '；'.join(parts) or '全部符合权限的记录'


def export_attendance(request, records, form):
    count = records.count(); ensure_size(count)
    totals = records.aggregate(clock=Sum('clock_days', default=0), leave=Sum('leave_days', default=0))
    scope = filter_scope(form)
    report = Report('月度打卡报表', scope)
    summary = report.sheet('报表汇总', [('项目', 30), ('数值 / 说明', 70)])
    for row in [('月度记录数', count), ('学生人数', records.order_by().values('student_id').distinct().count()),
                ('打卡天数合计', totals['clock']), ('请假天数合计', totals['leave']),
                ('总天数合计', totals['clock']+totals['leave']),
                ('统计说明', '导出全部筛选结果，忽略分页；总天数=打卡天数+请假天数，未导入月份不补零。')]:
        report.row(summary, row)
    monthly = report.sheet('按月汇总', [(label, 20) for label in ['月份', '记录数', '学生人数', '打卡天数', '请假天数', '总天数']])
    groups = records.order_by().values('month').annotate(rows=Count('pk'), people=Count('student_id', distinct=True), clock=Sum('clock_days'), leave=Sum('leave_days')).order_by('month')
    for row in groups:
        report.row(monthly, [row['month'], row['rows'], row['people'], row['clock'], row['leave'], row['clock']+row['leave']])
    columns = [('记录学号', 22), ('记录姓名', 20), ('当前学号', 22), ('当前姓名', 20), ('月份', 16),
        ('打卡天数', 14), ('请假天数', 14), ('总天数', 14), ('所在班级', 28), ('所在党支部', 30),
        ('政治面貌', 28), ('在校状态', 14), ('账号状态', 18), ('当前干部职务', 40), ('导入批次编号', 18),
        ('导入时间', 23), ('记录创建时间', 23), ('记录更新时间', 23)]
    details = report.sheet('月度打卡明细', columns)
    from .student_services import current_tenures
    roles = {}
    student_ids = records.order_by().values('student_id').distinct()
    for student_id, position in current_tenures().filter(student_id__in=student_ids).order_by('-starts_on', '-pk').values_list('student_id', 'position'):
        roles.setdefault(student_id, []).append(position)
    records = records.select_related('student', 'batch').only('student_id', 'student_name', 'student_number', 'month',
        'clock_days', 'leave_days', 'batch_id', 'created_at', 'updated_at', 'batch__created_at',
        *['student__'+field for field in ['username', 'full_name', 'student_class', 'party_branch', 'political_status', 'enrollment_status', 'is_archived', 'is_active']])
    for item in records.iterator(chunk_size=500):
        student = item.student
        report.row(details, [item.student_number, item.student_name or item.student_number, student.username, student.display_name,
            item.month, item.clock_days, item.leave_days, item.total_days, student.student_class, student.party_branch,
            student.get_political_status_display(), student.get_enrollment_status_display(), account_state(student),
            '、'.join(roles.get(item.student_id, [])), item.batch_id, local_datetime(item.batch.created_at),
            local_datetime(item.created_at), local_datetime(item.updated_at)])
    report.finish_sheet(monthly); report.finish_sheet(details)
    return report.response(request.user, count)


def export_students(request, records, form):
    count = records.count(); ensure_size(count)
    today = timezone.localdate(); month = today.replace(day=1)
    ids = records.order_by().values('pk')
    days = dict(StudentDailyActivity.objects.filter(student_id__in=ids, day__gte=month, day__lte=today).order_by().values_list('student_id').annotate(n=Count('pk')))
    counts = ContentVisitDaily.objects.filter(student_id__in=ids, day__lte=today).order_by().values('student_id').annotate(total=Sum('views'), month=Sum('views', filter=Q(day__gte=month)))
    clicks = {row['student_id']: row for row in counts}
    history = CadreTenure.objects.filter(student_id__in=ids)
    ensure_size(history.count())
    report = Report('学生信息报表', filter_scope(form))
    summary = report.sheet('报表汇总', [('项目', 30), ('数值 / 说明', 75)])
    for row in [('学生人数', count), ('在校人数', records.filter(enrollment_status='enrolled').count()),
                ('毕业人数', records.filter(enrollment_status='graduated').count()),
                ('已归档人数', records.filter(is_archived=True).count()), ('允许登录人数', records.filter(is_active=True, is_archived=False).count()),
                ('履职历史记录数', history.count()),
                ('统计说明', '基本资料及职务为导出时状态；包含完整身份证号；活跃率=本月活跃天数÷本月已过天数。历史访问不补算。')]:
        report.row(summary, row)
    columns = [('学号', 22), ('姓名', 20), ('身份证号', 25), ('在校状态', 14), ('所在党支部', 30), ('所在班级', 28),
        ('政治面貌', 28), ('院系', 25), ('账号状态', 18), ('在线情况', 30), ('当前干部职务', 42), ('履职历史条数', 18),
        ('最近活跃时间', 23), ('最近登录时间', 23), ('账号创建时间', 23), ('资料完整性', 16),
        ('本月活跃天数', 18), ('本月活跃率（%）', 20), ('本月内容点击量', 20), ('累计内容点击量', 20)]
    details = report.sheet('学生信息明细', columns)
    records = records.prefetch_related(None).prefetch_related(Prefetch('cadre_tenures', queryset=CadreTenure.objects.order_by('-starts_on', '-pk'), to_attr='report_tenures'))
    for item in records.iterator(chunk_size=500):
        roles = [role.position for role in item.report_tenures if role.starts_on <= today and (role.ends_on is None or role.ends_on >= today)]
        active_days = days.get(item.pk, 0); visits = clicks.get(item.pk, {})
        report.row(details, [item.username, item.display_name, item.id_number, item.get_enrollment_status_display(), item.party_branch,
            item.student_class, item.get_political_status_display(), item.department, account_state(item), item.presence['label'],
            '、'.join(roles), len(item.report_tenures), local_datetime(item.last_seen_at), local_datetime(item.last_login), local_datetime(item.date_joined),
            '完整' if item.profile_complete else '待补全', active_days, round(active_days/today.day*100, 1), visits.get('month') or 0, visits.get('total') or 0])
    tenure_sheet = report.sheet('干部履职历史', [(label, width) for label, width in [('学号', 22), ('姓名', 20), ('任职职务', 35), ('开始日期', 18), ('结束日期', 18), ('是否当前任职', 18), ('创建时间', 23), ('更新时间', 23)]])
    for role in history.select_related('student').order_by('student__username', '-starts_on', '-pk').iterator(chunk_size=500):
        current = role.starts_on <= today and (role.ends_on is None or role.ends_on >= today)
        report.row(tenure_sheet, [role.student.username, role.student.display_name, role.position, role.starts_on, role.ends_on,
            '是' if current else '否', local_datetime(role.created_at), local_datetime(role.updated_at)])
    report.finish_sheet(details); report.finish_sheet(tenure_sheet)
    return report.response(request.user, count)
