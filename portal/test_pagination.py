import csv
import io
from datetime import date, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import AttendanceBatch, CadreTenure, MonthlyAttendance, User
from .pagination import paginated_context
from .services import parse_attendance


class PaginationBoundsTests(SimpleTestCase):
    def context(self, query=None, rows=500, session=None, name="sample"):
        request = RequestFactory().get("/sample/", query or {})
        request.session = session if session is not None else {}
        request.resolver_match = SimpleNamespace(url_name=name)
        return paginated_context(request, range(rows))

    def test_custom_size_and_bounds(self):
        for size in [1, 20, 37, 100]:
            result = self.context({"per_page": size, "page": 2})
            self.assertEqual(list(result["page_obj"]), list(range(size, size * 2)))
            self.assertIn(size, result["page_size_options"])
        for value in ["", "0", "-1", "101", "100000000", "2.5", "abc", "9" * 5000]:
            self.assertEqual(self.context({"per_page": value})["per_page"], 12)

    def test_invalid_pages_and_empty_results_remain_usable(self):
        for page in ["0", "-3", "garbage", "2.5", "9" * 5000]:
            self.assertEqual(self.context({"page": page})["page_obj"].number, 1)
        self.assertEqual(self.context({"page": 999})["page_obj"].number, 42)
        empty = self.context({"page": 99, "per_page": 100}, rows=0)
        self.assertEqual((empty["page_obj"].number, empty["page_obj"].paginator.num_pages), (1, 1))
        self.assertEqual(list(empty["page_obj"]), [])

    def test_preferences_are_per_list_and_invalid_values_keep_valid_preference(self):
        session = {}
        self.context({"per_page": 37}, session=session)
        self.assertEqual(self.context(session=session)["per_page"], 37)
        self.assertEqual(self.context({"per_page": 0}, session=session)["per_page"], 37)
        self.assertEqual(self.context(session=session, name="other")["per_page"], 12)
        self.context({"per_page": 12}, session=session)
        self.assertEqual(self.context(session=session)["per_page"], 12)

    def test_repeated_filters_and_sort_survive_links_and_forms(self):
        result = self.context({"student_class": ["甲班", "乙班"], "q": "<学生&>", "sort": "clock_days", "per_page": 37, "page": 4})
        query = parse_qs(result["querystring"])
        self.assertEqual(query["student_class"], ["甲班", "乙班"])
        self.assertNotIn("page", query)
        self.assertEqual([value for name, value in result["pagination_filters"] if name == "student_class"], ["甲班", "乙班"])
        for link in result["page_links"]:
            if "url" in link:
                linked = parse_qs(urlsplit(link["url"]).query)
                self.assertEqual(linked["per_page"], ["37"])
                self.assertEqual(linked["sort"], ["clock_days"])
                self.assertEqual(linked["student_class"], ["甲班", "乙班"])

    def test_large_page_count_has_compact_navigation(self):
        result = self.context({"page": 500, "per_page": 1}, rows=10000)
        self.assertLessEqual(len(result["page_links"]), 9)
        self.assertTrue(any(link.get("ellipsis") for link in result["page_links"]))


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ListPaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(username="pages-manager", is_staff=True, full_name="管理员")
        cls.student = User.objects.create_user(username="pages-001", full_name="长姓名" * 22,
            id_number="110000199001010001", student_class="甲班")
        cls.other = User.objects.create_user(username="pages-002", full_name="学生乙", student_class="乙班")

    def setUp(self):
        self.client.force_login(self.admin)

    def test_all_business_lists_share_controls_for_both_roles(self):
        names = ["notification_list", "guide_list", "leave_list", "internship_list", "rental_list",
                 "faq_list", "consultation_list", "attendance_list", "policy_list", "video_list"]
        for user in [self.admin, self.student]:
            self.client.force_login(user)
            for name in names + (["account_list", "audit_list"] if user.is_staff else []):
                with self.subTest(user=user.username, page=name):
                    response = self.client.get(reverse(name), {"per_page": 37})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.context["page_obj"].paginator.per_page, 37)
                    self.assertContains(response, 'id="page-jump"')
                    self.assertContains(response, 'id="page-size"')
                    self.assertContains(response, '<option value="37" selected>37 条</option>')
                    self.assertContains(response, 'class="form-select pagination-page-size"')
                    self.assertNotContains(response, '<datalist')
                    # Shared controls stay available even with an empty result or one page.
                    self.assertContains(response, 'aria-label="分页"', count=1)

    def test_filter_forms_carry_custom_size_and_escape_query(self):
        for name in ["account_list", "notification_list", "guide_list", "faq_list", "consultation_list",
                     "leave_list", "internship_list", "rental_list", "video_list", "policy_list"]:
            response = self.client.get(reverse(name), {"q": '<script>"&', "per_page": 37})
            self.assertContains(response, 'name="per_page" value="37"')
            self.assertNotContains(response, '<script>"&')
        self.client.force_login(self.student)
        response = self.client.get(reverse("attendance_list"), {"per_page": 37})
        self.assertContains(response, 'name="per_page" value="37"')

    def test_student_multiselect_jump_keeps_filters_and_remembers_size(self):
        response = self.client.get(reverse("account_list"), {"student_class": ["甲班", "乙班"], "per_page": 1, "page": 2})
        self.assertEqual(len(response.context["page_obj"]), 1)
        self.assertEqual(response.context["page_obj"].paginator.count, 2)
        self.assertEqual(response.context["pagination_filters"].count(("student_class", "甲班")), 1)
        self.assertEqual(response.context["pagination_filters"].count(("student_class", "乙班")), 1)
        self.assertEqual(self.client.get(reverse("account_list")).context["per_page"], 1)
        self.assertEqual(self.client.get(reverse("notification_list")).context["per_page"], 12)

    def test_long_student_fields_are_bounded_and_identity_stays_masked(self):
        response = self.client.get(reverse("account_list"), {"q": self.student.username})
        self.assertContains(response, 'data-resizable-columns="students"')
        self.assertContains(response, 'data-column-resizer=', count=8)
        self.assertContains(response, 'title="' + self.student.full_name + '"')
        self.assertNotContains(response, self.student.id_number)
        self.assertContains(response, self.student.masked_id_number)
        self.assertContains(response, 'data-reset-columns="students"')

    def test_cadre_history_pagination_still_displays_all_current_jobs(self):
        today = timezone.localdate()
        CadreTenure.objects.bulk_create([CadreTenure(student=self.student, position=f"职务{i:02}",
            starts_on=today - timedelta(days=i), ends_on=date(2999, 12, 31)) for i in range(25)])
        response = self.client.get(reverse("account_detail", args=[self.student.pk]), {"per_page": 10, "page": 2})
        self.assertEqual(response.context["page_obj"].paginator.count, 25)
        self.assertEqual(len(response.context["tenures"]), 10)
        self.assertEqual(len(response.context["item"].current_tenures), 25)

    def test_monthly_pagination_uses_canonical_filters_sort_and_student_ownership(self):
        batch = AttendanceBatch.objects.create(uploaded_by=self.admin, row_count=24, is_confirmed=True)
        MonthlyAttendance.objects.bulk_create([MonthlyAttendance(student=user, student_number=user.username,
            student_name=user.full_name, month=date(2026, month, 1), clock_days=month, leave_days=0, batch=batch)
            for user in [self.student, self.other] for month in range(1, 13)])
        response = self.client.get(reverse("attendance_list"), {"year": 2026, "month": "", "per_page": 5, "page": 2, "sort": "clock_days", "direction": "asc"})
        query = parse_qs(response.context["querystring"])
        self.assertEqual(query["month_from"], ["2026-01"])
        self.assertEqual(query["month_to"], ["2026-12"])
        self.assertNotIn("year", query)
        self.assertEqual(len(response.context["page_obj"]), 5)
        for column in response.context["sort_columns"]:
            for link in column["links"]:
                self.assertEqual(parse_qs(urlsplit(link["url"]).query)["per_page"], ["5"])
                self.assertNotIn("page", parse_qs(urlsplit(link["url"]).query))
        self.client.force_login(self.student)
        response = self.client.get(reverse("attendance_list"), {"year": 2026, "month": "", "per_page": 5, "page": 2})
        self.assertEqual(response.context["page_obj"].paginator.count, 12)
        self.assertTrue(all(item.student_id == self.student.pk for item in response.context["page_obj"]))

    def test_small_preview_page_confirms_entire_import(self):
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["学号", "姓名", "月份", "打卡记录", "请假天数"])
        writer.writerows([[self.student.username, self.student.full_name, f"2026-{month:02}", 20, 1] for month in range(1, 13)])
        payload = parse_attendance(SimpleUploadedFile("monthly.csv", output.getvalue().encode("utf-8-sig")))
        batch = AttendanceBatch.objects.create(uploaded_by=self.admin, row_count=len(payload), payload=payload)
        url = reverse("attendance_preview", args=[batch.pk]) + "?per_page=5&page=2"
        response = self.client.get(url)
        self.assertEqual(len(response.context["page_obj"]), 5)
        self.assertContains(response, "每页预览 5 条")
        self.assertEqual(self.client.post(url, {"action": "confirm"}).status_code, 302)
        self.assertEqual(MonthlyAttendance.objects.filter(batch=batch).count(), 12)

    def test_small_delete_preview_preserves_full_selection(self):
        users = [User.objects.create_user(username=f"delete-page-{i}", full_name=f"删除测试{i}") for i in range(3)]
        response = self.client.post(reverse("account_delete_prepare") + "?per_page=1&q=delete-page-", {"selection_mode": "all"})
        response = self.client.get(response.url + "?per_page=1&page=2")
        self.assertEqual(len(response.context["page_obj"]), 1)
        self.assertEqual(response.context["total_count"], 3)
        self.assertEqual({pk for intent in self.client.session["student_delete_previews"].values() for pk in intent["ids"]}, {user.pk for user in users})
