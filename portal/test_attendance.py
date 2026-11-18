import csv
import io
import re
from datetime import date, timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from .models import AttendanceBatch, MonthlyAttendance, User
from .services import confirm_attendance, parse_attendance


HEADERS = ["学号", "姓名", "月份", "打卡记录", "请假天数"]


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class MonthlyAttendanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(username="attendance-admin", full_name="打卡管理员", is_staff=True)
        cls.other_admin = User.objects.create_user(username="attendance-admin-2", full_name="另一管理员", is_staff=True)
        cls.student = User.objects.create_user(username="0000123", full_name="学生甲")
        cls.other = User.objects.create_user(username="0000456", full_name="学生乙")

    def setUp(self):
        self.client.force_login(self.admin)

    def file(self, rows, headers=HEADERS):
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(headers)
        writer.writerows(rows)
        return SimpleUploadedFile("monthly.csv", output.getvalue().encode("utf-8-sig"))

    def row(self, **extra):
        values = {"username": self.student.username, "name": self.student.full_name, "month": "2026-11", "clock": "20", "leave": "2", **extra}
        return [values[name] for name in ["username", "name", "month", "clock", "leave"]]

    def batch(self, rows=None, **extra):
        payload = parse_attendance(self.file(rows or [self.row()]))
        return AttendanceBatch.objects.create(uploaded_by=self.admin, row_count=len(payload), payload=payload, **extra)

    def record(self, student=None, month=date(2026, 11, 1), clock=20, leave=2):
        student = student or self.student
        batch = AttendanceBatch.objects.create(uploaded_by=self.admin, row_count=1, is_confirmed=True)
        return MonthlyAttendance.objects.create(student=student, student_name=student.full_name, student_number=student.username,
            month=month, clock_days=clock, leave_days=leave, batch=batch)

    def test_template_exact_five_columns_bom_and_permissions(self):
        response = self.client.get(reverse("attendance_template"))
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")))), [HEADERS])
        self.assertIn("monthly-attendance-template.csv", response["Content-Disposition"])
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("attendance_template")).status_code, 403)

    def test_navigation_directly_below_support_for_both_roles(self):
        for user in [self.admin, self.student]:
            self.client.force_login(user)
            response = self.client.get(reverse("attendance_list"))
            nav = response.content.decode().split('<nav aria-label="主导航">')[1].split('</nav>')[0]
            links = re.findall(r'href="([^"]+)"', nav)
            self.assertEqual(links[links.index("/consultations/") + 1], "/attendance/")
            self.assertNotContains(response, "打卡状态")
            self.assertNotContains(response, "每日记录")
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("attendance_list")), "下载导入模板")

    def test_upload_has_no_separate_month_and_multiple_months_are_previewed(self):
        response = self.client.get(reverse("attendance_upload"))
        self.assertNotIn("month", response.context["form"].fields)
        self.assertContains(response, "非负整数")
        response = self.client.post(reverse("attendance_upload"), {"file": self.file([self.row(), self.row(month="2027-01")])})
        batch = AttendanceBatch.objects.get()
        self.assertRedirects(response, reverse("attendance_preview", args=[batch.pk]))
        self.assertIsNone(batch.month)
        self.assertFalse(MonthlyAttendance.objects.exists())
        response = self.client.get(reverse("attendance_preview", args=[batch.pk]))
        self.assertContains(response, "2026-11")
        self.assertContains(response, "2027-01")
        self.assertEqual(response.context["rows"][0]["total_days"], 22)

    def test_single_month_batch_and_confirmation_redirect(self):
        self.client.post(reverse("attendance_upload"), {"file": self.file([self.row()])})
        batch = AttendanceBatch.objects.get()
        self.assertEqual(batch.month, date(2026, 11, 1))
        response = self.client.post(reverse("attendance_preview", args=[batch.pk]), {"action": "confirm"})
        self.assertRedirects(response, reverse("attendance_list") + "?year=2026&month=11")
        self.assertEqual(MonthlyAttendance.objects.get().total_days, 22)

    def test_zero_and_month_length_leap_year_validation(self):
        rows = parse_attendance(self.file([self.row(clock="0", leave="0"), self.row(month="2024-02", clock="28", leave="1")]))
        self.assertEqual([row["total_days"] for row in rows], [0, 29])
        for month, clock, leave in [("2026-02", "28", "1"), ("2026-04", "30", "1"), ("2026-01", "31", "1")]:
            with self.subTest(month=month), self.assertRaisesMessage(ValidationError, "不能超过该月实际天数"):
                parse_attendance(self.file([self.row(month=month, clock=clock, leave=leave)]))

    def test_counts_must_be_integers_and_nonnegative(self):
        for field in ["clock", "leave"]:
            for value in ["", "-1", "0.5", "1.0", "abc", "NaN", "Infinity", "1e1", "999999", "２０"]:
                with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                    parse_attendance(self.file([self.row(**{field: value})]))

    def test_month_format_and_year_bounds(self):
        for value in ["", "2026-1", "2026-00", "2026-13", "202611", "2026-11-01", "1899-01", "0000-01", "20260-01"]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                parse_attendance(self.file([self.row(month=value)]))

    def test_name_account_role_active_and_leading_zero_checks(self):
        for extra in [{"name": ""}, {"name": "其他姓名"}, {"username": "123"}, {"username": self.admin.username}]:
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                parse_attendance(self.file([self.row(**extra)]))
        self.student.is_active = False
        self.student.save()
        with self.assertRaises(ValidationError):
            parse_attendance(self.file([self.row()]))

    def test_duplicate_person_month_rejected_but_other_month_allowed(self):
        with self.assertRaisesMessage(ValidationError, "同一学号和月份重复"):
            parse_attendance(self.file([self.row(), self.row(clock="19")]))
        self.assertEqual(len(parse_attendance(self.file([self.row(), self.row(month="2026-12")]))), 2)

    def test_headers_column_count_encoding_nul_and_empty_file(self):
        files = [self.file([], headers=["学号", "日期", "状态", "备注"]), self.file([]), self.file([self.row()[:-1]]),
                 self.file([self.row() + ["额外列"]]), SimpleUploadedFile("bad.csv", b"\xff\xfe"),
                 SimpleUploadedFile("nul.csv", b"\x00")]
        for upload in files:
            with self.subTest(upload=upload.name), self.assertRaises(ValidationError):
                parse_attendance(upload)

    def test_invalid_last_row_prevents_any_preview_or_record(self):
        response = self.client.post(reverse("attendance_upload"), {"file": self.file([self.row(), self.row(month="2026-12", name="错误姓名")])})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "第 3 行")
        self.assertFalse(AttendanceBatch.objects.exists())
        self.assertFalse(MonthlyAttendance.objects.exists())

    def test_row_and_file_size_limits(self):
        with self.assertRaisesMessage(ValidationError, "最多导入 5000 行"):
            parse_attendance(self.file([self.row()] * 5001))
        upload = SimpleUploadedFile("huge.csv", b"x" * (2 * 1024 * 1024 + 1))
        with self.assertRaisesMessage(ValidationError, "不超过 2 MB"):
            parse_attendance(upload)
        response = self.client.post(reverse("attendance_upload"), {"file": SimpleUploadedFile("wrong.xlsx", b"data")})
        self.assertIn("file", response.context["form"].errors)

    def test_1500_rows_one_account_lookup_pagination_and_whole_batch_confirmation(self):
        User.objects.bulk_create([User(username=f"bulk-{i:04}", full_name=f"学生{i}") for i in range(1500)], batch_size=500)
        rows = [[f"bulk-{i:04}", f"学生{i}", "2026-11", "20", "2"] for i in range(1500)]
        with CaptureQueriesContext(connection) as queries:
            payload = parse_attendance(self.file(rows))
        self.assertEqual(len(queries), 1)
        batch = AttendanceBatch.objects.create(uploaded_by=self.admin, row_count=1500, payload=payload)
        response = self.client.get(reverse("attendance_preview", args=[batch.pk]), {"page": 2})
        self.assertEqual(len(response.context["rows"]), 100)
        self.assertEqual(response.context["rows"][0]["username"], "bulk-0100")
        self.client.post(reverse("attendance_preview", args=[batch.pk]) + "?page=2", {"action": "confirm"})
        self.assertEqual(MonthlyAttendance.objects.count(), 1500)
        self.assertEqual(MonthlyAttendance.objects.get(student_number="bulk-1499").total_days, 22)

    def test_reimport_replaces_same_month_only_and_confirmation_idempotent(self):
        previous = self.record()
        other_month = self.record(month=date(2026, 10, 1), clock=15, leave=1)
        other_student = self.record(student=self.other, clock=18, leave=2)
        batch = self.batch([self.row(clock="21", leave="4")])
        confirm_attendance(batch.pk, self.admin)
        previous.refresh_from_db()
        other_month.refresh_from_db()
        other_student.refresh_from_db()
        self.assertEqual((previous.clock_days, previous.leave_days, previous.total_days), (21, 4, 25))
        self.assertEqual((other_month.total_days, other_student.total_days), (16, 20))
        self.assertEqual(MonthlyAttendance.objects.count(), 3)
        with self.assertRaises(ValidationError):
            confirm_attendance(batch.pk, self.admin)
        batch.refresh_from_db()
        self.assertEqual(batch.payload, [])

    def test_preview_belongs_to_uploader_and_expiry_legacy_and_permissions_enforced(self):
        batch = self.batch()
        url = reverse("attendance_preview", args=[batch.pk])
        self.client.force_login(self.other_admin)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {"action": "confirm"}).status_code, 404)
        with self.assertRaises(ValidationError):
            confirm_attendance(batch.pk, self.other_admin)
        with self.assertRaises(ValidationError):
            confirm_attendance(batch.pk, self.student)
        self.client.force_login(self.admin)
        AttendanceBatch.objects.filter(pk=batch.pk).update(created_at=timezone.now() - timedelta(days=2))
        with self.assertRaisesMessage(ValidationError, "已过期"):
            confirm_attendance(batch.pk, self.admin)
        self.assertContains(self.client.get(url), "预览已超过一天")
        old = AttendanceBatch.objects.create(uploaded_by=self.admin, row_count=1, format_version=1, payload=[{"date": "2026-11-01"}])
        self.assertContains(self.client.get(reverse("attendance_preview", args=[old.pk])), "旧版每日打卡预览")
        with self.assertRaisesMessage(ValidationError, "旧版每日打卡预览"):
            confirm_attendance(old.pk, self.admin)
        self.assertFalse(MonthlyAttendance.objects.exists())

    def test_changed_account_after_preview_rejects_whole_batch(self):
        for change in [{"full_name": "姓名更正"}, {"username": "new-id"}, {"is_staff": True}, {"is_active": False}]:
            batch = self.batch()
            User.objects.filter(pk=self.student.pk).update(**change)
            with self.assertRaisesMessage(ValidationError, "发生变化"):
                confirm_attendance(batch.pk, self.admin)
            self.assertFalse(MonthlyAttendance.objects.exists())
            User.objects.filter(pk=self.student.pk).update(full_name="学生甲", username="0000123", is_staff=False, is_active=True)

    def test_cancel_and_invalid_action_never_writes_data(self):
        batch = self.batch()
        url = reverse("attendance_preview", args=[batch.pk])
        self.client.post(url, {"action": "unexpected"})
        self.assertFalse(MonthlyAttendance.objects.exists())
        self.client.post(url, {"action": "cancel"})
        self.assertFalse(AttendanceBatch.objects.filter(pk=batch.pk).exists())
        self.assertFalse(MonthlyAttendance.objects.exists())

    def test_csrf_required_on_confirmation(self):
        batch = self.batch()
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.admin)
        self.assertEqual(strict.post(reverse("attendance_preview", args=[batch.pk]), {"action": "confirm"}).status_code, 403)
        self.assertFalse(MonthlyAttendance.objects.exists())

    def test_year_month_student_query_and_totals_do_not_include_other_records(self):
        wanted = self.record()
        self.record(month=date(2025, 11, 1), clock=10, leave=1)
        self.record(month=date(2026, 10, 1), clock=15, leave=3)
        self.record(student=self.other, clock=18, leave=2)
        response = self.client.get(reverse("attendance_list"), {"year": 2026, "month": 11, "q": self.student.username})
        self.assertEqual(list(response.context["page_obj"]), [wanted])
        self.assertEqual(response.context["totals"], [("打卡天数", 20), ("请假天数", 2), ("总天数", 22)])
        self.assertContains(response, "22 天")
        response = self.client.get(reverse("attendance_list"), {"year": 2026, "month": "", "q": self.student.full_name})
        self.assertEqual(response.context["page_obj"].paginator.count, 2)
        self.assertEqual(response.context["totals"][-1], ("总天数", 40))

    def test_students_cannot_filter_into_others_or_see_their_names_or_totals(self):
        wanted = self.record()
        self.record(student=self.other, clock=1, leave=1)
        self.client.force_login(self.student)
        response = self.client.get(reverse("attendance_list"), {"year": 2026, "month": 11, "q": self.other.username, "student": self.other.pk})
        self.assertEqual(list(response.context["page_obj"]), [wanted])
        self.assertNotContains(response, self.other.full_name)
        self.assertNotContains(response, self.other.username)
        self.assertEqual(response.context["totals"][-1], ("总天数", 22))

    def test_no_record_is_empty_rather_than_fake_zero_and_invalid_filters_not_fallback(self):
        self.record()
        for params in [{"year": 2026, "month": 12}, {"year": "bad", "month": 11}, {"year": 2026, "month": 13},
                       {"year": 2026, "month": "2026-00"}, {"year": 10000, "month": 1}]:
            response = self.client.get(reverse("attendance_list"), params)
            self.assertEqual(response.context["page_obj"].paginator.count, 0)
        self.assertEqual(self.client.get(reverse("attendance_list"), {"year": 9999, "month": ""}).status_code, 200)

    def test_old_month_link_works_and_pagination_preserves_filters(self):
        self.record()
        self.assertEqual(len(self.client.get(reverse("attendance_list"), {"month": "2026-11"}).context["page_obj"]), 1)
        for month in range(1, 11):
            self.record(month=date(2026, month, 1), clock=10, leave=1)
        self.record(student=self.other)
        self.record(student=self.other, month=date(2026, 10, 1))
        response = self.client.get(reverse("attendance_list"), {"year": 2026, "month": "", "q": "学生", "page": 2})
        self.assertEqual(response.context["page_obj"].paginator.count, 13)
        self.assertEqual(len(response.context["page_obj"]), 1)
        self.assertIn("month_from=2026-01", response.context["querystring"])
        self.assertIn("month_to=2026-12", response.context["querystring"])

    def filtered(self, **extra):
        return self.client.get(reverse("attendance_list"), {"month_from": "", "month_to": "", **extra})

    def test_admin_month_range_includes_endpoints_and_crosses_years(self):
        self.record(month=date(2025, 11, 1))
        first = self.record(month=date(2025, 12, 1), clock=12, leave=1)
        last = self.record(month=date(2026, 1, 1), clock=13, leave=2)
        self.record(month=date(2026, 2, 1))
        response = self.filtered(month_from="2025-12", month_to="2026-01", q=self.student.username)
        self.assertEqual(list(response.context["page_obj"]), [last, first])
        self.assertEqual(response.context["totals"], [("打卡天数", 25), ("请假天数", 3), ("总天数", 28)])
        self.assertEqual(response.context["student_count"], 1)
        self.assertNotIn("year", response.context["filter_form"].fields)
        self.assertNotIn("month", response.context["filter_form"].fields)

    def test_range_filters_support_open_ends_and_explicit_all_months(self):
        older = self.record(month=date(2025, 12, 1))
        newer = self.record(month=date(2026, 1, 1))
        cases = [({"month_from": "2026-01"}, [newer]), ({"month_to": "2025-12"}, [older]), ({}, [newer, older]),
                 ({"month_from": "2025-12", "month_to": "2025-12"}, [older])]
        for params, expected in cases:
            with self.subTest(params=params):
                self.assertEqual(list(self.filtered(**params).context["page_obj"]), expected)

    def test_admin_default_current_month_is_preserved_in_sort_links(self):
        with patch("portal.views.timezone.localdate", return_value=date(2026, 11, 17)):
            wanted = self.record()
            self.record(month=date(2026, 10, 1))
            response = self.client.get(reverse("attendance_list"))
        self.assertEqual(list(response.context["page_obj"]), [wanted])
        form = response.context["filter_form"]
        self.assertEqual((form["month_from"].value(), form["month_to"].value()), ("2026-11", "2026-11"))
        for column in response.context["sort_columns"]:
            for link in column["links"]:
                self.assertEqual(parse_qs(urlsplit(link["url"]).query)["month_from"], ["2026-11"])

    def test_all_three_day_categories_zero_exact_and_one_sided_bounds(self):
        a = self.record(clock=20, leave=2)
        b = self.record(student=self.other, clock=18, leave=6)
        zero = self.record(month=date(2026, 12, 1), clock=0, leave=0)
        cases = [({"day_kind": "clock_days", "days_min": 18, "days_max": 20}, {a.pk, b.pk}),
                 ({"day_kind": "leave_days", "days_min": 2, "days_max": 2}, {a.pk}),
                 ({"day_kind": "total_days", "days_min": 24, "days_max": 24}, {b.pk}),
                 ({"day_kind": "total_days", "days_min": 0, "days_max": 0}, {zero.pk}),
                 ({"day_kind": "clock_days", "days_min": 19}, {a.pk}),
                 ({"day_kind": "clock_days", "days_max": 18}, {b.pk, zero.pk})]
        for params, expected in cases:
            with self.subTest(params=params):
                self.assertEqual({item.pk for item in self.filtered(**params).context["page_obj"]}, expected)

    def test_day_bounds_apply_to_each_month_not_accumulated_range(self):
        self.record(month=date(2026, 10, 1), clock=20, leave=2)
        self.record(clock=20, leave=2)
        self.record(student=self.other, clock=19, leave=3)
        response = self.filtered(month_from="2026-10", month_to="2026-11", day_kind="clock_days", days_min=20, days_max=20,
                                 q=self.student.username)
        self.assertEqual(response.context["page_obj"].paginator.count, 2)
        self.assertEqual(response.context["student_count"], 1)
        self.assertEqual(response.context["totals"][-1], ("总天数", 44))

    def test_invalid_ranges_days_categories_and_sort_do_not_fall_back_to_all(self):
        self.record()
        cases = [{"month_from": "2026-12", "month_to": "2026-11"}, {"month_from": "2026-13"},
                 {"month_to": "1899-12"}, {"month_from": "2026-11-01"}, {"month_to": "10000-01"},
                 {"days_min": 4, "days_max": 3}, {"days_min": -1}, {"days_max": 32}, {"days_min": "1.5"},
                 {"days_max": "bad"}, {"day_kind": "password"}, {"sort": "student__password"},
                 {"sort": "month;DROP TABLE"}, {"direction": "sideways"}]
        for params in cases:
            with self.subTest(params=params):
                response = self.filtered(**params)
                self.assertContains(response, "筛选或排序条件有误")
                self.assertEqual(response.context["page_obj"].paginator.count, 0)
                self.assertEqual(response.context["totals"][-1], ("总天数", 0))
        self.assertEqual(self.filtered(month_to="9999-12").status_code, 200)

    def test_each_column_sorts_both_directions_with_numeric_total(self):
        students = [User.objects.create(username=number, full_name=name) for number, name in [("001", "B"), ("010", "A"), ("002", "C"), ("000", "")]]
        a = self.record(students[0], date(2026, 1, 1), clock=2, leave=5)
        b = self.record(students[1], date(2026, 2, 1), clock=10, leave=0)
        c = self.record(students[2], date(2025, 12, 1), clock=3, leave=1)
        d = self.record(students[3], date(2025, 11, 1), clock=1, leave=2)
        expected = {"student_number": [d, a, c, b], "student_name": [d, b, a, c], "month": [d, c, a, b],
                    "clock_days": [d, a, c, b], "leave_days": [b, c, d, a], "total_days": [d, c, a, b]}
        for column, ordered in expected.items():
            for direction in ["asc", "desc"]:
                with self.subTest(column=column, direction=direction):
                    response = self.filtered(sort=column, direction=direction)
                    self.assertEqual(list(response.context["page_obj"]), ordered if direction == "asc" else ordered[::-1])
                    self.assertContains(response, f'aria-sort="{"ascending" if direction == "asc" else "descending"}"', count=1)

    def test_sort_links_reset_page_keep_filters_and_have_accessible_triangles(self):
        response = self.filtered(month_from="2025-09", month_to="2026-12", day_kind="total_days", days_min=2, days_max=25,
                                 q="学生 甲", sort="total_days", direction="asc", page=3)
        self.assertEqual(len(response.context["sort_columns"]), 6)
        self.assertContains(response, 'class="sort-triangle asc"', count=6)
        self.assertContains(response, 'class="sort-triangle desc"', count=6)
        self.assertContains(response, 'aria-label="按总天数升序排列"')
        for column in response.context["sort_columns"]:
            for link in column["links"]:
                query = parse_qs(urlsplit(link["url"]).query)
                self.assertNotIn("page", query)
                self.assertEqual(query["q"], ["学生 甲"])
                self.assertEqual(query["month_from"], ["2025-09"])
                self.assertEqual(query["days_min"], ["2"])
                self.assertEqual(query["day_kind"], ["total_days"])
        self.assertContains(response, 'name="sort" value="total_days"')
        self.assertContains(response, 'name="direction" value="asc"')

    def test_global_order_and_totals_cover_all_pages_and_ties_are_stable(self):
        records = [self.record(month=date(year, month, 1), clock=10, leave=1) for year in [2025, 2026] for month in range(1, 13)]
        params = {"month_from": "2025-01", "month_to": "2026-12", "sort": "clock_days", "direction": "asc", "days_min": 10, "days_max": 10}
        first = self.client.get(reverse("attendance_list"), params)
        second = self.client.get(reverse("attendance_list"), {**params, "page": 2})
        ordered = list(first.context["page_obj"]) + list(second.context["page_obj"])
        self.assertEqual(ordered, records[::-1])
        self.assertEqual(first.context["totals"], [("打卡天数", 240), ("请假天数", 24), ("总天数", 264)])
        self.assertEqual(first.context["student_count"], 1)
        self.assertEqual(parse_qs(first.context["querystring"])["sort"], ["clock_days"])
        self.assertIn("month_to=2026-12", first.context["querystring"])

    def test_sorting_and_forged_admin_filters_cannot_bypass_student_isolation(self):
        wanted = self.record()
        self.record(student=self.other, clock=1, leave=1)
        self.client.force_login(self.student)
        response = self.filtered(year=2026, month=11, day_kind="total_days", days_min=0, days_max=2,
                                 q=self.other.username, sort="total_days", direction="desc")
        self.assertEqual(list(response.context["page_obj"]), [wanted])
        self.assertNotContains(response, self.other.username)
        self.assertNotContains(response, self.other.full_name)
        self.assertNotIn("month_from", response.context["filter_form"].fields)
        self.assertEqual(response.context["totals"][-1], ("总天数", 22))
        self.assertNotIn("days_min", parse_qs(response.context["querystring"]))

    def test_anonymous_cannot_access_list_upload_preview_template(self):
        batch = self.batch()
        self.client.logout()
        for name, args in [("attendance_list", []), ("attendance_upload", []), ("attendance_preview", [batch.pk]), ("attendance_template", [])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 302)
