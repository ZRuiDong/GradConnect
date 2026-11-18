import csv
import io
from datetime import datetime
from django.contrib.auth.hashers import make_password
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from .leave_reports import EXPORT_HEADERS
from .models import Audit, Leave, User
from .services import transition_leave


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class LeaveReportTests(TestCase):
    @classmethod
    def moment(cls, value):
        return timezone.make_aware(datetime.fromisoformat(value))

    @classmethod
    def setUpTestData(cls):
        password = make_password("Tests!safe2026")
        cls.admin = User.objects.create(username="manager", full_name="管理员", is_staff=True, password=password)
        cls.student = User.objects.create(username="0000123", full_name="学生甲", department="计算机学院", password=password)
        cls.other = User.objects.create(username="20260002", full_name="学生乙", password=password)
        cases = [
            ("之前", "2026-11-09T00:00", "2026-11-09T23:59:59", "pending", cls.student),
            ("触及开始", "2026-11-09T20:00", "2026-11-10T00:00", "pending", cls.student),
            ("开始当天", "2026-11-10T00:00", "2026-11-10T08:00", "pending", cls.student),
            ("跨越全段", "2026-11-01T00:00", "2026-11-30T00:00", "approved", cls.student),
            ("范围之内", "2026-11-11T08:00", "2026-11-11T17:00", "approved", cls.student),
            ("结束当天", "2026-11-12T23:59:59.999999", "2026-11-13T01:00", "pending", cls.student),
            ("之后", "2026-11-13T00:00", "2026-11-14T00:00", "pending", cls.student),
            ("另一学生", "2026-11-11T08:00", "2026-11-11T17:00", "approved", cls.other),
        ]
        cls.items = {}
        for reason, start, end, status, student in cases:
            cls.items[reason] = Leave.objects.create(student=student, kind="personal", starts_at=cls.moment(start),
                ends_at=cls.moment(end), reason=reason, destination="本市", contact="013800000000", status=status)
        # 提交日期与请假日期不同，确保按业务起止时间筛选。
        Leave.objects.all().update(created_at=cls.moment("2026-01-01T12:00"))

    def setUp(self):
        self.client.force_login(self.admin)

    def query(self, **params):
        return self.client.get(reverse("leave_list"), params)

    def reasons(self, response):
        return {item.reason for item in response.context["page_obj"]}

    def csv_rows(self, params=None):
        response = self.client.get(reverse("leave_export"), params or {})
        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content)
        response.close()
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        return response, list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline="")))

    def test_date_overlap_includes_boundaries_and_spanning_leave(self):
        response = self.query(start_date="2026-11-10", end_date="2026-11-12")
        self.assertEqual(self.reasons(response), {"触及开始", "开始当天", "跨越全段", "范围之内", "结束当天", "另一学生"})
        self.assertContains(response, 'value="2026-11-10"')
        self.assertContains(response, "北京时间")

    def test_open_ended_ranges(self):
        self.assertEqual(self.reasons(self.query(start_date="2026-11-13")), {"结束当天", "之后", "跨越全段"})
        self.assertEqual(self.reasons(self.query(end_date="2026-11-09")), {"之前", "触及开始", "跨越全段"})

    def test_same_day_is_inclusive(self):
        response = self.query(start_date="2026-11-12", end_date="2026-11-12")
        self.assertEqual(self.reasons(response), {"跨越全段", "结束当天"})

    def test_status_keyword_and_dates_combine(self):
        response = self.query(start_date="2026-11-10", end_date="2026-11-12", status="approved", q="0000123")
        self.assertEqual(self.reasons(response), {"跨越全段", "范围之内"})
        self.assertEqual(self.reasons(self.query(q="学生乙")), {"另一学生"})

    def test_student_filter_always_scoped_to_self(self):
        self.client.force_login(self.student)
        response = self.query(start_date="2026-11-10", end_date="2026-11-12", student=self.other.pk)
        self.assertNotIn("另一学生", self.reasons(response))
        self.assertNotContains(response, "导出筛选结果")
        self.assertEqual(self.reasons(self.query(q="20260002")), set())

    def test_invalid_filters_do_not_fall_back_to_all_records(self):
        for params in [{"start_date": "not-a-date"}, {"end_date": "2026-02-30"},
                       {"start_date": "2026-11-12", "end_date": "2026-11-01"},
                       {"status": "unknown"}, {"q": "x" * 201}]:
            with self.subTest(params=params):
                response = self.query(**params)
                self.assertContains(response, "筛选条件有误")
                self.assertEqual(self.reasons(response), set())
                export = self.client.get(reverse("leave_export"), params)
                self.assertEqual(export.status_code, 400)
                self.assertNotIn("Content-Disposition", export)
        self.assertContains(self.query(start_date="2026-11-12", end_date="2026-11-01"), "结束日期不能早于开始日期")

    def test_export_is_admin_only_and_get_only(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("leave_export")).status_code, 302)
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("leave_export"), {"is_staff": "1"}).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(reverse("leave_export")).status_code, 405)

    def test_export_matches_list_filters_and_has_local_times(self):
        params = {"start_date": "2026-11-10", "end_date": "2026-11-12", "status": "pending", "q": "0000123"}
        response, rows = self.csv_rows(params)
        self.assertEqual({row["请假原因"] for row in rows}, self.reasons(self.query(**params)))
        self.assertIn('attachment; filename="leave-records-', response["Content-Disposition"])
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(rows[0]["学号"], "0000123")
        row = next(row for row in rows if row["请假原因"] == "开始当天")
        self.assertEqual(row["开始时间"], "2026-11-10 00:00:00")
        self.assertEqual(row["结束时间"], "2026-11-10 08:00:00")
        self.assertEqual(row["联系电话"], "013800000000")

    def test_export_includes_all_pages(self):
        for index in range(18):
            Leave.objects.create(student=self.student, kind="sick", starts_at=self.moment("2026-11-11T09:00"),
                ends_at=self.moment("2026-11-11T10:00"), reason=f"分页记录{index}", destination="本市", contact="13800000000")
        params = {"q": "分页记录", "page": "2", "start_date": "2026-11-11", "end_date": "2026-11-11"}
        page = self.query(**params)
        self.assertEqual(page.context["page_obj"].paginator.count, 18)
        self.assertEqual(len(page.context["page_obj"]), 6)
        self.assertIn("start_date=2026-11-11", page.context["querystring"])
        self.assertEqual(len(self.csv_rows(params)[1]), 18)

    def test_review_and_return_details_export(self):
        item = self.items["开始当天"]
        transition_leave(item.pk, self.admin, "approve", '同意，注意安全\n请及时返校 "确认"')
        transition_leave(item.pk, self.admin, "close", "已核实线下销假")
        _, rows = self.csv_rows({"status": "closed"})
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["办理状态"], "已销假")
        self.assertEqual(row["最近审核人"], "管理员")
        self.assertEqual(row["审核意见"], '同意，注意安全\n请及时返校 "确认"')
        self.assertEqual(row["销假确认人"], "管理员")
        self.assertEqual(row["销假说明"], "已核实线下销假")
        self.assertTrue(row["最近审核时间"])
        self.assertTrue(row["销假时间"])

    def test_csv_escapes_delimiters_newlines_and_formula_prefixes(self):
        item = self.items["开始当天"]
        item.reason = '返乡,处理事务\n第二行 "说明"'
        item.destination = '=HYPERLINK("https://example.invalid")'
        item.contact = "+8613800000000"
        item.save()
        self.student.full_name = "  @SUM(1,2)"
        self.student.save()
        _, rows = self.csv_rows({"q": "第二行"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["请假原因"], item.reason)
        self.assertEqual(rows[0]["离校去向"], "'" + item.destination)
        self.assertEqual(rows[0]["联系电话"], "'" + item.contact)
        self.assertEqual(rows[0]["姓名"], "'" + self.student.full_name)

    def test_empty_export_has_header_and_export_is_logged(self):
        response, rows = self.csv_rows({"q": "没有这条记录"})
        self.assertEqual(rows, [])
        event = Audit.objects.get(action="请求导出请假数据")
        self.assertEqual(event.actor_id, self.admin.pk)
        self.assertIn("没有这条记录", event.target)

    def test_export_header_and_previous_application(self):
        item = self.items["开始当天"]
        item.previous = self.items["之前"]
        item.save()
        _, rows = self.csv_rows({"q": "开始当天"})
        self.assertEqual(list(rows[0]), EXPORT_HEADERS)
        self.assertEqual(rows[0]["原申请编号"], str(item.previous_id))

    def test_latest_possible_end_date_does_not_overflow(self):
        self.assertEqual(len(self.reasons(self.query(end_date="9999-12-31"))), 8)
        self.assertEqual(len(self.reasons(self.query(start_date="0001-01-01"))), 8)

    def test_head_download_does_not_create_audit_record(self):
        response = self.client.head(reverse("leave_export"))
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertFalse(Audit.objects.filter(action="请求导出请假数据").exists())
