import csv
import io
import re
from datetime import date
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from .forms import STUDENT_IMPORT_HEADERS
from .models import Audit, CadreTenure, Conversation, Message, StudentImportBatch, User
from .services import confirm_student_accounts, parse_student_accounts
from .student_services import delete_students, student_identity_digest


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class StudentInformationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(username="student-manager", full_name="管理员", is_staff=True)
        cls.other_admin = User.objects.create_user(username="other-manager", full_name="另一管理员", is_staff=True)
        cls.student = User.objects.create_user(username="000001", full_name="学生甲", password="Student!initial2026",
            id_number="110000199001010001", graduation_month=date(2028, 6, 1), party_branch="第一党支部", student_class="甲班", political_status="league")
        cls.other = User.objects.create_user(username="000002", full_name="学生乙", graduation_month=date(2027, 6, 1),
            party_branch="无", student_class="乙班", political_status="mass")

    def setUp(self):
        self.client.force_login(self.admin)

    def data(self, **extra):
        return {"username": "000003", "full_name": "新学生", "id_number": "11000019900101000x", "enrollment_status": "enrolled",
                "party_branch": "无", "student_class": "甲班", "political_status": "", "department": "研究生院", "is_active": "on",
                "password1": "Initial!Maple2026", "password2": "Initial!Maple2026", **extra}

    def edit_data(self, **extra):
        token = self.client.get(reverse("account_edit", args=[self.student.pk])).context["edit_token"]
        return {**self.data(**extra), "edit_token": token}

    def tenure_data(self, rows, initial=0):
        data = {"tenures-TOTAL_FORMS": str(len(rows)), "tenures-INITIAL_FORMS": str(initial),
                "tenures-MIN_NUM_FORMS": "0", "tenures-MAX_NUM_FORMS": "100"}
        for index, row in enumerate(rows):
            data.update({f"tenures-{index}-{key}": value for key, value in row.items()})
        return data

    def tenure(self, student=None, position="班长", start=date(2026, 9, 1), end=None):
        return CadreTenure.objects.create(student=student or self.student, position=position, starts_on=start, ends_on=end)

    def prepare(self, ids=None, query=None, mode="selected"):
        query = query or {}
        url = reverse("account_delete_prepare")
        if query:
            from urllib.parse import urlencode
            url += "?" + urlencode(query, doseq=True)
        response = self.client.post(url, {"ids": ids or [self.student.pk], "selection_mode": mode})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/delete/previews/", response.url)
        return response.url

    def test_navigation_name_position_and_student_hidden(self):
        page = self.client.get(reverse("account_list"))
        nav = page.content.decode().split('<nav aria-label="主导航">')[1].split('</nav>')[0]
        links = re.findall(r'href="([^"]+)"', nav)
        self.assertEqual(links[links.index("/attendance/") + 1], "/accounts/")
        self.assertContains(page, "学生信息汇总")
        self.client.force_login(self.student)
        self.assertNotContains(self.client.get(reverse("dashboard")), "学生信息汇总")

    def test_required_fields_for_create_and_edit(self):
        for field in ["username", "full_name", "id_number", "enrollment_status"]:
            data = self.data()
            data[field] = ""
            response = self.client.post(reverse("account_create"), data)
            self.assertIn(field, response.context["form"].errors)
        self.assertFalse(User.objects.filter(username="000003").exists())
        response = self.client.post(reverse("account_edit", args=[self.student.pk]), self.edit_data(full_name=""))
        self.assertIn("full_name", response.context["form"].errors)

    def test_creation_optional_politics_no_tenure_and_privilege_forgery_ignored(self):
        response = self.client.post(reverse("account_create"), self.data(is_staff="on", is_superuser="on", is_archived="on"))
        item = User.objects.get(username="000003")
        self.assertRedirects(response, reverse("account_detail", args=[item.pk]))
        self.assertTrue(item.profile_complete)
        self.assertEqual(item.id_number, "11000019900101000X")
        self.assertEqual(item.enrollment_status, "enrolled")
        self.assertFalse(item.is_staff or item.is_superuser or item.is_archived)
        self.assertFalse(item.cadre_tenures.exists())
        self.assertEqual(item.political_status, "")

    def test_all_six_political_choices_and_invalid_value(self):
        self.assertEqual(list(User.Politics.labels), ["群众", "共青团员", "共青团员（入党积极分子）", "共青团员（发展对象）", "预备党员", "正式党员"])
        for index, value in enumerate(User.Politics.values):
            self.assertEqual(self.client.post(reverse("account_create"), self.data(username=f"politics-{index}", political_status=value)).status_code, 302)
        response = self.client.post(reverse("account_create"), self.data(political_status="invalid"))
        self.assertIn("political_status", response.context["form"].errors)

    def test_id_and_graduation_validation(self):
        for number in ["bad", "123456789012345", "110000199013010001", "110000299001010001"]:
            response = self.client.post(reverse("account_create"), self.data(id_number=number))
            self.assertIn("id_number", response.context["form"].errors)
        self.assertIn("enrollment_status", self.client.post(reverse("account_create"), self.data(enrollment_status="invalid")).context["form"].errors)

    def test_create_multiple_tenures_latest_only_in_table_and_history_in_detail(self):
        rows = [{"position": "旧职务", "starts_on": "2024-09-01", "ends_on": "2025-08-31"},
                {"position": "最新职务", "starts_on": "2026-09-01", "ends_on": ""}]
        self.client.post(reverse("account_create"), {**self.data(), **self.tenure_data(rows)})
        item = User.objects.get(username="000003")
        self.assertEqual(item.cadre_tenures.count(), 2)
        page = self.client.get(reverse("account_list"), {"q": item.username})
        table = page.content.decode().split('<tbody>')[1].split('</tbody>')[0]
        self.assertIn("最新职务", table)
        self.assertNotIn("旧职务", table)
        detail = self.client.get(reverse("account_detail", args=[item.pk]))
        self.assertContains(detail, "旧职务")
        self.assertContains(detail, "2999-12-31")

    def test_tenure_validation_blocks_entire_profile_save_and_bad_management(self):
        rows = [{"position": "班长", "starts_on": "2026-09-01", "ends_on": "2025-09-01"}]
        response = self.client.post(reverse("account_create"), {**self.data(), **self.tenure_data(rows)})
        self.assertContains(response, "结束日期不能早于开始日期")
        self.assertFalse(User.objects.filter(username="000003").exists())
        response = self.client.post(reverse("account_create"), {**self.data(), "tenures-0-position": "非法行"})
        self.assertTrue(response.context["tenure_formset"].non_form_errors())
        self.assertFalse(User.objects.filter(username="000003").exists())

    def test_edit_and_remove_tenures_with_username_immutable(self):
        first = self.tenure(position="旧班长", start=date(2025, 9, 1))
        second = self.tenure(position="班长")
        rows = [{"id": first.pk, "position": first.position, "starts_on": "2025-09-01", "DELETE": "on"},
                {"id": second.pk, "position": "学生会主席", "starts_on": "2026-09-01", "ends_on": ""}]
        response = self.client.post(reverse("account_edit", args=[self.student.pk]), {**self.edit_data(username="forged-id"), **self.tenure_data(rows, initial=2)})
        self.assertEqual(response.status_code, 302)
        self.student.refresh_from_db()
        self.assertEqual(self.student.username, "000001")
        self.assertFalse(CadreTenure.objects.filter(pk=first.pk).exists())
        self.assertEqual(self.student.cadre_tenures.get().position, "学生会主席")

    def test_tenure_foreign_id_cannot_modify_other_students_record(self):
        own = self.tenure()
        other = self.tenure(student=self.other, position="其他学生的职务")
        rows = [{"id": other.pk, "position": "伪造修改", "starts_on": "2026-09-01", "ends_on": ""}]
        self.client.post(reverse("account_edit", args=[self.student.pk]), {**self.edit_data(), **self.tenure_data(rows, initial=1)})
        other.refresh_from_db()
        self.assertEqual(other.position, "其他学生的职务")
        self.assertTrue(CadreTenure.objects.filter(pk=own.pk).exists())

    def test_multi_category_filter_and_date_range_intersection(self):
        self.tenure(position="班长")
        response = self.client.get(reverse("account_list"), {"student_class": ["甲班", "乙班"], "party_branch": ["第一党支部"],
            "political_status": ["league"], "enrollment_status": "enrolled", "current_position": ["班长"]})
        self.assertEqual(list(response.context["page_obj"]), [self.student])
        self.assertEqual(self.client.get(reverse("account_list"), {"student_class": ["乙班"]}).context["page_obj"].paginator.count, 1)

    def test_latest_role_filter_does_not_match_historical_role(self):
        self.tenure(position="学习委员", start=date(2025, 9, 1), end=date(2026, 8, 31))
        self.tenure(position="班长")
        self.assertEqual(self.client.get(reverse("account_list"), {"current_position": ["学习委员"]}).context["page_obj"].paginator.count, 0)
        self.assertEqual(self.client.get(reverse("account_list"), {"cadre": "yes"}).context["page_obj"].paginator.count, 1)
        self.assertEqual(self.client.get(reverse("account_list"), {"completeness": "incomplete"}).context["page_obj"].paginator.count, 1)

    def test_combined_search_matches_across_fields_and_requires_all_terms(self):
        self.student.department = "计算机学院"
        self.student.save()
        self.tenure(position="历史职务", start=date(2025, 9, 1), end=date(2026, 8, 31))
        self.tenure(position="班长")
        for query in ["甲班 班长", "计算机 第一党支部", "000001 学生甲"]:
            response = self.client.get(reverse("account_list"), {"q": query})
            self.assertEqual(list(response.context["page_obj"]), [self.student])
        for query in ["历史职务", "乙班 班长"]:
            self.assertEqual(self.client.get(reverse("account_list"), {"q": query}).context["page_obj"].paginator.count, 0)
        self.assertEqual(self.client.get(reverse("account_list"), {"q": "班长", "student_class": ["乙班"]}).context["page_obj"].paginator.count, 0)

    def test_filter_chip_removal_retains_other_conditions_and_resets_pagination(self):
        from urllib.parse import parse_qs, urlsplit
        response = self.client.get(reverse("account_list"), {"student_class": ["甲班", "乙班"], "political_status": ["league", "mass"],
            "enrollment_status": "enrolled", "page": 2})
        chips = response.context["filter_chips"]
        class_chip = next(chip for chip in chips if chip["label"] == "甲班")
        params = parse_qs(urlsplit(class_chip["url"]).query)
        self.assertEqual(params["student_class"], ["乙班"])
        self.assertEqual(params["political_status"], ["league", "mass"])
        self.assertEqual(params["enrollment_status"], ["enrolled"])
        self.assertNotIn("page", params)
        self.assertEqual(list(self.client.get(class_chip["url"]).context["page_obj"]), [self.other])
        range_chip = next(chip for chip in chips if chip["label"] == "在校")
        params = parse_qs(urlsplit(range_chip["url"]).query)
        self.assertNotIn("enrollment_status", params)
        self.assertEqual(params["student_class"], ["甲班", "乙班"])

    def test_bad_filter_never_expands_to_all_and_pagination_preserves_multi_values(self):
        for params in [{"student_class": ["不存在"]}, {"political_status": ["invalid"]}, {"enrollment_status": "invalid"}]:
            self.assertEqual(self.client.get(reverse("account_list"), params).context["page_obj"].paginator.count, 0)
        for index in range(15):
            User.objects.create(username=f"page-{index}", full_name="分页学生", student_class="甲班", party_branch="无")
        response = self.client.get(reverse("account_list"), {"student_class": ["甲班", "乙班"], "page": 2})
        self.assertEqual(response.context["page_obj"].paginator.count, 17)
        self.assertEqual(len(response.context["page_obj"]), 5)
        self.assertEqual(response.context["querystring"].count("student_class="), 2)

    def test_id_masked_in_list_and_preview_only_admin_detail_shows_full(self):
        page = self.client.get(reverse("account_list"))
        self.assertNotContains(page, self.student.id_number)
        self.assertContains(page, self.student.masked_id_number)
        self.assertContains(self.client.get(reverse("account_detail", args=[self.student.pk])), self.student.id_number)
        self.assertEqual(page["Cache-Control"], "private, no-store")

    def test_new_import_template_required_fields_and_profile_values(self):
        self.assertEqual(STUDENT_IMPORT_HEADERS, ["学号", "姓名", "身份证号", "在校状态", "所在党支部", "所在班级", "政治面貌", "院系", "初始密码"])
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(STUDENT_IMPORT_HEADERS)
        writer.writerow(["new-import", "新导入学生", "11000019900101000x", "在校", "无", "甲班", "预备", "学院", "Initial!Maple2026"])
        payload = parse_student_accounts(SimpleUploadedFile("students.csv", output.getvalue().encode("utf-8-sig")))
        batch = StudentImportBatch.objects.create(uploaded_by=self.admin, row_count=1, payload=payload)
        preview = self.client.get(reverse("account_preview", args=[batch.pk]))
        self.assertContains(preview, "预备党员")
        self.assertNotContains(preview, "11000019900101000X")
        confirm_student_accounts(batch.pk, self.admin)
        item = User.objects.get(username="new-import")
        self.assertTrue(item.profile_complete)
        self.assertEqual(item.political_status, "probationary")

    def test_old_pending_import_cannot_bypass_required_profile_fields(self):
        batch = StudentImportBatch.objects.create(uploaded_by=self.admin, row_count=1, payload=[{"username": "old-preview"}], format_version=1)
        self.assertContains(self.client.get(reverse("account_preview", args=[batch.pk])), "旧版预览")
        with self.assertRaisesMessage(ValidationError, "旧版账号导入预览"):
            confirm_student_accounts(batch.pk, self.admin)

    def update_upload(self, usernames=None, **extra):
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(STUDENT_IMPORT_HEADERS)
        for username in usernames or [self.student.username]:
            values = {"name": "更新后的学生", "id_number": "11000019900101000X", "status": "毕业", "branch": "无",
                      "class_name": "更新班级", "politics": "群众", "password": "", **extra}
            writer.writerow([username, values["name"], values["id_number"], values["status"], values["branch"], values["class_name"], values["politics"], "更新院系", values["password"]])
        return SimpleUploadedFile("update.csv", output.getvalue().encode("utf-8-sig"))

    def test_existing_profile_bulk_update_preserves_password_permissions_archive_and_tenures(self):
        self.tenure()
        User.objects.filter(pk=self.student.pk).update(is_active=False, is_archived=True)
        original_password = self.student.password
        with self.assertNumQueries(1), patch("portal.services.make_password") as hasher:
            rows = parse_student_accounts(self.update_upload(), mode="update")
        hasher.assert_not_called()
        self.assertNotIn("password", rows[0])
        self.assertNotIn("password_hash", rows[0])
        rows[0].update(is_staff=True, is_superuser=True, is_active=True, is_archived=False)
        batch = StudentImportBatch.objects.create(uploaded_by=self.admin, mode="update", row_count=1, payload=rows)
        confirm_student_accounts(batch.pk, self.admin)
        self.student.refresh_from_db()
        self.assertEqual(self.student.full_name, "更新后的学生")
        self.assertEqual(self.student.student_class, "更新班级")
        self.assertEqual(self.student.password, original_password)
        self.assertFalse(self.student.is_active or self.student.is_staff or self.student.is_superuser)
        self.assertTrue(self.student.is_archived)
        self.assertEqual(self.student.cadre_tenures.count(), 1)

    def test_profile_update_rejects_unknown_admin_password_and_missing_fields(self):
        for usernames, extra in [(["missing"], {}), ([self.admin.username], {}), (None, {"password": "Unexpected!Maple2026"}),
                                 (None, {"name": ""}), (None, {"id_number": ""})]:
            with self.subTest(usernames=usernames, extra=extra), self.assertRaises(ValidationError):
                parse_student_accounts(self.update_upload(usernames, **extra), mode="update")
        self.student.refresh_from_db()
        self.assertEqual(self.student.student_class, "甲班")

    def test_update_preview_identity_change_rejects_entire_batch(self):
        rows = parse_student_accounts(self.update_upload([self.student.username, self.other.username]), mode="update")
        batch = StudentImportBatch.objects.create(uploaded_by=self.admin, mode="update", row_count=2, payload=rows)
        User.objects.filter(pk=self.other.pk).update(username="renamed")
        with self.assertRaisesMessage(ValidationError, "本次未更新任何资料"):
            confirm_student_accounts(batch.pk, self.admin)
        self.student.refresh_from_db()
        self.assertEqual(self.student.student_class, "甲班")

    def test_update_mode_http_form_preview_confirmation(self):
        response = self.client.post(reverse("account_upload"), {"mode": "update", "file": self.update_upload()})
        batch = StudentImportBatch.objects.get()
        self.assertEqual(batch.mode, "update")
        self.assertRedirects(response, reverse("account_preview", args=[batch.pk]))
        self.assertContains(self.client.get(response.url), "确认更新 1 名学生资料")
        self.client.post(response.url, {"action": "confirm"})
        self.student.refresh_from_db()
        self.assertEqual(self.student.student_class, "更新班级")

    def test_1500_existing_profiles_update_without_changing_password_or_status(self):
        User.objects.bulk_create([User(username=f"update-{i:04}", full_name="原姓名", password="original-hash", is_active=False) for i in range(1500)], batch_size=200)
        rows = parse_student_accounts(self.update_upload([f"update-{i:04}" for i in range(1500)]), mode="update")
        batch = StudentImportBatch.objects.create(uploaded_by=self.admin, mode="update", row_count=1500, payload=rows)
        confirm_student_accounts(batch.pk, self.admin)
        updated = User.objects.filter(username__startswith="update-")
        self.assertEqual(updated.filter(student_class="更新班级", password="original-hash", is_active=False).count(), 1500)

    def partial_upload(self, headers, rows):
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(headers)
        writer.writerows(rows)
        return SimpleUploadedFile("partial.csv", output.getvalue().encode("utf-8-sig"))

    def partial_batch(self, headers, values):
        rows = parse_student_accounts(self.partial_upload(headers, values), mode="update")
        return StudentImportBatch.objects.create(uploaded_by=self.admin, mode="update", row_count=len(rows), payload=rows)

    def test_party_branch_and_class_optional_for_create_edit_and_completeness_filter(self):
        response = self.client.post(reverse("account_create"), self.data(party_branch="", student_class=""))
        created = User.objects.get(username="000003")
        self.assertRedirects(response, reverse("account_detail", args=[created.pk]))
        self.assertTrue(created.profile_complete)
        self.client.post(reverse("account_edit", args=[self.student.pk]), self.edit_data(party_branch="", student_class=""))
        self.student.refresh_from_db()
        self.assertEqual(self.student.party_branch, "")
        self.assertEqual(self.student.student_class, "")
        self.assertTrue(self.student.profile_complete)
        response = self.client.get(reverse("account_list"), {"party_branch": "__empty__", "completeness": "complete"})
        self.assertEqual({item.pk for item in response.context["page_obj"]}, {created.pk, self.student.pk})
        self.assertNotIn(self.student.pk, {item.pk for item in self.client.get(reverse("account_list"), {"completeness": "incomplete"}).context["page_obj"]})

    def test_optional_party_branch_new_import_and_legacy_department_header(self):
        headers = ["院系/班级" if header == "院系" else header for header in STUDENT_IMPORT_HEADERS]
        upload = self.partial_upload(headers, [["legacy-template", "导入学生", "110000199001010001", "在校", "", "唯一班级", "", "独立学院", "Initial!Maple2026"]])
        rows = parse_student_accounts(upload)
        batch = StudentImportBatch.objects.create(uploaded_by=self.admin, row_count=1, payload=rows)
        confirm_student_accounts(batch.pk, self.admin)
        item = User.objects.get(username="legacy-template")
        self.assertEqual((item.party_branch, item.student_class, item.department), ("", "唯一班级", "独立学院"))
        self.assertTrue(item.profile_complete)

    def test_create_import_with_blank_or_omitted_class(self):
        for omit_class in (False, True):
            with self.subTest(omit_class=omit_class):
                headers = list(STUDENT_IMPORT_HEADERS)
                username = f"optional-class-{int(omit_class)}"
                values = [username, "导入学生", "110000199001010001", "在校", "", "", "", "学院", "Initial!Maple2026"]
                if omit_class:
                    values.pop(headers.index("所在班级"))
                    headers.remove("所在班级")
                upload = self.partial_upload(headers, [values])
                response = self.client.post(reverse("account_upload"), {"mode": "create", "file": upload})
                self.assertEqual(response.status_code, 302)
                self.assertContains(self.client.get(response.url), "<small>未填写</small>", html=True)
                self.assertRedirects(self.client.post(response.url, {"action": "confirm"}), reverse("account_list"))
                item = User.objects.get(username=username)
                self.assertEqual((item.student_class, item.department), ("", "学院"))
                self.assertTrue(item.profile_complete)

    def test_update_import_can_clear_existing_class(self):
        batch = self.partial_batch(["学号", "所在班级"], [[self.student.username, ""]])
        confirm_student_accounts(batch.pk, self.admin)
        self.student.refresh_from_db()
        self.assertEqual(self.student.student_class, "")
        self.assertTrue(self.student.profile_complete)

    def test_politics_template_and_partial_http_update_preserve_other_data(self):
        self.assertEqual(list(csv.reader(io.StringIO(self.client.get(reverse("account_template")).content.decode("utf-8-sig"))))[0], STUDENT_IMPORT_HEADERS)
        template = self.client.get(reverse("account_template"), {"mode": "update"})
        self.assertEqual(list(csv.reader(io.StringIO(template.content.decode("utf-8-sig")))), [["学号", "政治面貌"]])
        self.assertContains(self.client.get(reverse("account_upload")), "下载政治面貌更新模板")
        before = User.objects.values().get(pk=self.student.pk)
        response = self.client.post(reverse("account_upload"), {"mode": "update", "file": self.partial_upload(["学号", "政治面貌"], [[self.student.username, "预备党员"]])})
        preview = self.client.get(response.url)
        self.assertContains(preview, "本次仅更新：<strong>政治面貌</strong>", html=False)
        self.assertContains(preview, self.student.masked_id_number)
        self.assertNotContains(preview, self.student.id_number)
        self.assertContains(preview, "预备党员")
        self.assertEqual(User.objects.get(pk=self.student.pk).political_status, "league")
        self.assertRedirects(self.client.post(response.url, {"action": "confirm"}), reverse("account_list"))
        after = User.objects.values().get(pk=self.student.pk)
        self.assertEqual({**before, "political_status": "probationary"}, after)

    def test_partial_update_allows_unselected_incomplete_legacy_fields(self):
        old = User.objects.create_user(username="incomplete", full_name="", password="Unchanged!Maple2026")
        batch = self.partial_batch(["学号", "政治面貌"], [[old.username, "正式党员"]])
        preview = self.client.get(reverse("account_preview", args=[batch.pk]))
        self.assertContains(preview, "待补全")
        self.assertNotContains(preview, "********")
        confirm_student_accounts(batch.pk, self.admin)
        old.refresh_from_db()
        self.assertEqual((old.full_name, old.id_number, old.graduation_month, old.political_status), ("", "", None, "party"))
        self.assertTrue(old.check_password("Unchanged!Maple2026"))

    def test_partial_optional_clear_omitted_columns_and_arbitrary_header_order(self):
        batch = self.partial_batch(["政治面貌", "学号", "所在党支部", "初始密码"], [["", self.student.username, "", ""]])
        confirm_student_accounts(batch.pk, self.admin)
        self.student.refresh_from_db()
        self.assertEqual((self.student.political_status, self.student.party_branch), ("", ""))
        self.assertEqual((self.student.full_name, self.student.student_class), ("学生甲", "甲班"))

    def test_partial_update_rejects_bad_headers_values_and_duplicate_students(self):
        cases = [(["学号"], [[self.student.username]]),
                 (["政治面貌"], [["群众"]]),
                 (["学号", "政治面貌", "政治面貌"], [[self.student.username, "群众", "正式党员"]]),
                 (["学号", "院系", "院系/班级"], [[self.student.username, "学院", "学院"]]),
                 (["学号", "is_staff"], [[self.student.username, "1"]]),
                 (["学号", "姓名"], [[self.student.username, ""]]),
                 (["学号", "身份证号"], [[self.student.username, "bad"]]),
                 (["学号", "在校状态"], [[self.student.username, "2028-13"]]),
                 (["学号", "政治面貌"], [[self.student.username, "错误类别"]]),
                 (["学号", "政治面貌", "初始密码"], [[self.student.username, "群众", "Changed!Maple2026"]]),
                 (["学号", "政治面貌"], [[self.admin.username, "群众"]]),
                 (["学号", "政治面貌"], [[self.student.username, "群众"], [self.student.username, "群众"]])]
        for headers, rows in cases:
            with self.subTest(headers=headers, rows=rows), self.assertRaises(ValidationError):
                parse_student_accounts(self.partial_upload(headers, rows), mode="update")
        self.assertFalse(StudentImportBatch.objects.exists())

    def test_partial_conflict_rejects_entire_batch_after_other_profile_change(self):
        batch = self.partial_batch(["学号", "政治面貌"], [[self.student.username, "群众"], [self.other.username, "正式党员"]])
        User.objects.filter(pk=self.other.pk).update(student_class="管理员新班级")
        with self.assertRaisesMessage(ValidationError, "本次未更新任何资料"):
            confirm_student_accounts(batch.pk, self.admin)
        self.assertEqual(User.objects.get(pk=self.student.pk).political_status, "league")
        batch.refresh_from_db()
        self.assertFalse(batch.is_confirmed)

    def test_partial_update_cannot_change_permissions_via_preview_fields(self):
        batch = self.partial_batch(["学号", "政治面貌"], [[self.student.username, "正式党员"]])
        batch.payload[0]["update_fields"] = ["is_staff"]
        batch.payload[0]["is_staff"] = True
        batch.save(update_fields=["payload"])
        with self.assertRaisesMessage(ValidationError, "预览更新字段无效"):
            confirm_student_accounts(batch.pk, self.admin)
        self.assertFalse(User.objects.get(pk=self.student.pk).is_staff)

    def test_full_update_preview_without_column_metadata_remains_compatible(self):
        rows = parse_student_accounts(self.update_upload(), mode="update")
        rows[0].pop("update_fields")
        batch = StudentImportBatch.objects.create(uploaded_by=self.admin, mode="update", row_count=1, payload=rows)
        confirm_student_accounts(batch.pk, self.admin)
        self.assertEqual(User.objects.get(pk=self.student.pk).student_class, "更新班级")

    def test_1500_partial_updates_preserve_all_other_fields_without_password_hashing(self):
        User.objects.bulk_create([User(username=f"partial-{i:04}", full_name="原学生", password="original-hash", student_class="原班级", party_branch="", is_active=False) for i in range(1500)], batch_size=200)
        upload = self.partial_upload(["学号", "政治面貌"], [[f"partial-{i:04}", "共青团员（发展对象）"] for i in range(1500)])
        with self.assertNumQueries(1), patch("portal.services.make_password") as hasher:
            rows = parse_student_accounts(upload, mode="update")
        hasher.assert_not_called()
        batch = StudentImportBatch.objects.create(uploaded_by=self.admin, mode="update", row_count=1500, payload=rows)
        confirm_student_accounts(batch.pk, self.admin)
        self.assertEqual(User.objects.filter(username__startswith="partial-", full_name="原学生", password="original-hash", student_class="原班级", party_branch="", is_active=False, political_status="candidate", id_number="", graduation_month__isnull=True).count(), 1500)

    def test_single_unassociated_student_delete_requires_review_and_explicit_confirm(self):
        url = self.prepare()
        self.assertTrue(User.objects.filter(pk=self.student.pk).exists())
        self.assertContains(self.client.get(url), "彻底删除 1 名")
        self.assertEqual(self.client.post(url, {"confirmed": ""}).status_code, 400)
        self.assertTrue(User.objects.filter(pk=self.student.pk).exists())
        self.assertRedirects(self.client.post(url, {"confirmed": "on"}), reverse("account_list"))
        self.assertFalse(User.objects.filter(pk=self.student.pk).exists())
        self.assertEqual(self.client.post(url, {"confirmed": "on"}).status_code, 404)

    def test_associated_student_archives_preserves_business_records_and_invalidates_session(self):
        thread = Conversation.objects.create(student=self.student, subject="保留咨询")
        Message.objects.create(conversation=thread, author=self.student, body="保留原文")
        student_client = Client()
        self.assertTrue(student_client.login(username=self.student.username, password="Student!initial2026"))
        url = self.prepare()
        self.client.post(url, {"confirmed": "on"})
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_archived)
        self.assertFalse(self.student.is_active)
        self.assertEqual(Message.objects.get(conversation=thread).body, "保留原文")
        self.assertEqual(student_client.get(reverse("dashboard")).status_code, 302)
        self.assertNotIn(self.student, self.client.get(reverse("account_list")).context["page_obj"])
        self.assertIn(self.student, self.client.get(reverse("account_list"), {"archive": "archived"}).context["page_obj"])
        self.client.post(reverse("account_status", args=[self.student.pk, "restore"]))
        self.client.post(reverse("account_status", args=[self.student.pk, "enable"]))
        self.assertEqual(student_client.get(reverse("dashboard")).status_code, 302)
        self.assertTrue(student_client.login(username=self.student.username, password="Student!initial2026"))

    def test_tenure_history_alone_causes_archive_and_restore_stays_disabled(self):
        self.tenure()
        self.client.post(self.prepare(), {"confirmed": "on"})
        self.assertTrue(CadreTenure.objects.filter(student=self.student).exists())
        self.client.post(reverse("account_status", args=[self.student.pk, "restore"]))
        self.student.refresh_from_db()
        self.assertFalse(self.student.is_archived or self.student.is_active)
        self.client.post(reverse("account_status", args=[self.student.pk, "enable"]))
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_active)

    def test_cannot_enable_archived_student_via_status_or_edit(self):
        User.objects.filter(pk=self.student.pk).update(is_archived=True, is_active=False)
        self.assertEqual(self.client.post(reverse("account_status", args=[self.student.pk, "enable"])).status_code, 404)
        response = self.client.post(reverse("account_edit", args=[self.student.pk]), self.edit_data(is_active="on"))
        self.assertIn("is_active", response.context["form"].errors)
        self.student.refresh_from_db()
        self.assertFalse(self.student.is_active)

    def test_disable_and_reset_remain_available_for_old_incomplete_accounts(self):
        self.client.post(reverse("account_status", args=[self.other.pk, "disable"]))
        self.other.refresh_from_db()
        self.assertFalse(self.other.is_active)
        response = self.client.post(reverse("account_reset", args=[self.other.pk]), {"new_password1": "Reset!Maple2026", "new_password2": "Reset!Maple2026"})
        self.assertEqual(response.status_code, 302)
        self.other.refresh_from_db()
        self.assertTrue(self.other.check_password("Reset!Maple2026"))

    def test_bulk_delete_filtered_across_pages_keeps_unmatched_and_ignores_forged_confirm_ids(self):
        User.objects.bulk_create([User(username=f"bulk-{index:04}", full_name="批量学生", student_class="待处理班") for index in range(1500)], batch_size=200)
        url = self.prepare(query={"student_class": ["待处理班"]}, mode="all")
        response = self.client.get(url)
        self.assertEqual(response.context["total_count"], 1500)
        self.assertEqual(len(response.context["page_obj"]), 100)
        self.client.post(url, {"confirmed": "on", "ids": [self.student.pk]})
        self.assertFalse(User.objects.filter(username__startswith="bulk-").exists())
        self.assertTrue(User.objects.filter(pk=self.student.pk).exists())
        self.assertEqual(Audit.objects.filter(action="删除学生（无业务记录）").count(), 1500)

    def test_bulk_selected_mixed_association_delete_and_archive(self):
        self.tenure()
        self.client.post(self.prepare([self.student.pk, self.other.pk]), {"confirmed": "on"})
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_archived)
        self.assertFalse(User.objects.filter(pk=self.other.pk).exists())

    def test_mismatched_filters_admin_ids_or_invalid_ids_never_delete(self):
        for data in [{"ids": [self.admin.pk]}, {"ids": ["bad"]}, {"ids": [str(2**70)]}]:
            self.client.post(reverse("account_delete_prepare"), data)
        self.client.post(reverse("account_delete_prepare") + "?student_class=乙班", {"ids": [self.student.pk]})
        self.assertTrue(User.objects.filter(pk=self.student.pk).exists())
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())

    def test_delete_cancel_ownership_expiration_and_csrf(self):
        url = self.prepare()
        self.client.force_login(self.other_admin)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.admin)
        url = self.prepare()
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.admin)
        self.assertEqual(strict.post(reverse("account_delete_prepare"), {"ids": [self.student.pk]}).status_code, 403)
        self.client.post(url, {"action": "cancel"})
        self.assertTrue(User.objects.filter(pk=self.student.pk).exists())
        self.assertEqual(self.client.get(url).status_code, 404)
        url = self.prepare()
        session = self.client.session
        previews = session["student_delete_previews"]
        for intent in previews.values():
            intent["created"] = timezone.now().timestamp() - 1801
        session["student_delete_previews"] = previews
        session.save()
        self.assertEqual(self.client.post(url, {"confirmed": "on"}).status_code, 404)

    def test_identity_change_after_review_aborts_whole_deletion(self):
        url = self.prepare([self.student.pk, self.other.pk])
        User.objects.filter(pk=self.other.pk).update(full_name="更正姓名")
        self.assertContains(self.client.post(url, {"confirmed": "on"}), "名单或学生身份信息已经变化", status_code=400)
        self.assertEqual(User.objects.filter(pk__in=[self.student.pk, self.other.pk]).count(), 2)

    def test_transaction_rollback_keeps_both_delete_and_archive_targets(self):
        self.tenure()
        students = [self.student, self.other]
        with patch("portal.student_services.Audit.objects.bulk_create", side_effect=IntegrityError("test rollback")):
            with self.assertRaises(IntegrityError):
                delete_students([item.pk for item in students], self.admin, student_identity_digest(students))
        self.student.refresh_from_db()
        self.assertFalse(self.student.is_archived)
        self.assertTrue(User.objects.filter(pk=self.other.pk).exists())

    def test_students_and_anonymous_cannot_read_or_modify_any_student_information(self):
        paths = [reverse("account_list"), reverse("account_detail", args=[self.student.pk]), reverse("account_edit", args=[self.student.pk]),
                 reverse("account_create"), reverse("account_delete_prepare"), reverse("account_status", args=[self.student.pk, "disable"])]
        self.client.force_login(self.student)
        for path in paths:
            self.assertEqual(self.client.get(path).status_code, 403)
            self.assertEqual(self.client.post(path, self.data()).status_code, 403)
        self.client.logout()
        for path in paths:
            self.assertEqual(self.client.get(path).status_code, 302)
