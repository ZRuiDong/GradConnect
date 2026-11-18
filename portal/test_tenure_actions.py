from datetime import date
from unittest.mock import patch

from django.db import IntegrityError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import Audit, CadreTenure, User


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class TenureActionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin=User.objects.create_user(username="tenure-manager",is_staff=True)
        cls.other_admin=User.objects.create_user(username="tenure-manager-2",is_staff=True)
        cls.student=User.objects.create_user(username="000201",full_name="学生甲",id_number="110000199001010001",
            student_class="甲班",password="Keep!password2026")
        cls.other=User.objects.create_user(username="000202",full_name="学生乙")
        cls.tenure=CadreTenure.objects.create(student=cls.student,position="班长",starts_on=date(2026,9,1))

    def setUp(self):
        self.client.force_login(self.admin)
        self.url=reverse("account_tenure_action",args=[self.student.pk])
        self.token=self.client.get(reverse("account_edit",args=[self.student.pk])).context["edit_token"]

    def payload(self, **extra):
        return {"action":"save","confirmed":"yes","edit_token":self.token,"position":"学习委员",
                "starts_on":"2026-10-01","ends_on":"",**extra}

    def test_page_rows_and_detail_actions_are_present(self):
        edit=self.client.get(reverse("account_edit",args=[self.student.pk]))
        self.assertContains(edit,'data-tenure-manager')
        self.assertContains(edit,'data-tenure-action="edit"')
        self.assertContains(edit,'data-tenure-action="delete"')
        self.assertContains(edit,'data-tenure-action="save"')
        self.assertContains(edit,'data-tenure-action="cancel"')
        detail=self.client.get(reverse("account_detail",args=[self.student.pk]))
        self.assertContains(detail,f'#tenure-{self.tenure.pk}')
        self.assertContains(detail,'data-tenure-delete-form')
        self.assertContains(detail,'data-confirm=')

    def test_permissions_post_only_and_explicit_confirmation(self):
        self.assertEqual(self.client.get(self.url).status_code,405)
        for extra in [{"confirmed":""},{"action":"invalid"}]:
            self.assertEqual(self.client.post(self.url,self.payload(**extra)).status_code,400)
        self.assertEqual(self.student.cadre_tenures.count(),1)
        self.client.force_login(self.student)
        self.assertEqual(self.client.post(self.url,self.payload()).status_code,403)
        self.client.logout()
        self.assertEqual(self.client.post(self.url,self.payload()).status_code,302)
        self.client.force_login(self.admin)
        admin_url=reverse("account_tenure_action",args=[self.admin.pk])
        self.assertEqual(self.client.post(admin_url,self.payload()).status_code,404)

    def test_create_defaults_and_does_not_modify_profile_or_other_records(self):
        before=User.objects.values().get(pk=self.student.pk)
        response=self.client.post(self.url,self.payload(full_name="不能修改",is_staff="1",student_id=self.other.pk))
        self.assertEqual(response.status_code,200)
        item=CadreTenure.objects.get(pk=response.json()["saved_id"])
        self.assertEqual((item.student_id,item.position,item.ends_on),(self.student.pk,"学习委员",date(2999,12,31)))
        self.assertEqual(User.objects.values().get(pk=self.student.pk),before)
        self.assertNotEqual(response.json()["edit_token"],self.token)
        self.assertIn('tenures-TOTAL_FORMS',response.json()["html"])
        self.assertTrue(Audit.objects.filter(action="新增干部履职记录",actor=self.admin).exists())

    def test_update_refreshes_token_and_global_profile_can_save_afterwards(self):
        response=self.client.post(self.url,self.payload(tenure_id=self.tenure.pk))
        self.assertEqual(response.status_code,200)
        self.tenure.refresh_from_db();self.assertEqual(self.tenure.position,"学习委员")
        form={"username":self.student.username,"full_name":"学生甲已核对","id_number":self.student.id_number,
              "student_class":"甲班","enrollment_status":"enrolled","is_active":"on","edit_token":self.token}
        edit_url=reverse("account_edit",args=[self.student.pk])
        self.assertEqual(self.client.post(edit_url,form).status_code,409)
        form["edit_token"]=response.json()["edit_token"]
        form.update({"tenures-TOTAL_FORMS":"1","tenures-INITIAL_FORMS":"1","tenures-MAX_NUM_FORMS":"100",
            "tenures-0-id":str(self.tenure.pk),"tenures-0-position":"学习委员","tenures-0-starts_on":"2026-10-01","tenures-0-ends_on":"2999-12-31"})
        self.assertEqual(self.client.post(edit_url,form).status_code,302)
        self.student.refresh_from_db();self.assertEqual(self.student.full_name,"学生甲已核对")
        self.assertEqual(self.student.cadre_tenures.count(),1)

    def test_invalid_fields_leave_all_data_unchanged(self):
        for fields in [{"position":""},{"starts_on":""},{"starts_on":"2026-12-01","ends_on":"2026-01-01"}]:
            response=self.client.post(self.url,self.payload(tenure_id=self.tenure.pk,**fields))
            self.assertEqual(response.status_code,400)
            self.assertTrue(response.json()["errors"])
        self.tenure.refresh_from_db()
        self.assertEqual(self.tenure.position,"班长")
        self.assertEqual(self.student.cadre_tenures.count(),1)

    def test_foreign_record_ids_cannot_be_modified_or_deleted(self):
        other=CadreTenure.objects.create(student=self.other,position="另一学生班长",starts_on=date(2026,1,1))
        for action in ["save","delete"]:
            self.assertEqual(self.client.post(self.url,self.payload(action=action,tenure_id=other.pk)).status_code,404)
        other.refresh_from_db();self.assertEqual(other.position,"另一学生班长")
        self.assertTrue(CadreTenure.objects.filter(pk=self.tenure.pk).exists())

    def test_stale_missing_wrong_actor_tokens_reject_without_mutation(self):
        self.assertEqual(self.client.post(self.url,self.payload(edit_token="")).status_code,409)
        self.client.force_login(self.other_admin)
        self.assertEqual(self.client.post(self.url,self.payload()).status_code,409)
        self.client.force_login(self.admin)
        User.objects.filter(pk=self.student.pk).update(full_name="另一管理员更新")
        response=self.client.post(self.url,self.payload())
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.json()["reload_url"],reverse("account_edit",args=[self.student.pk]))
        self.assertEqual(self.student.cadre_tenures.count(),1)

    def test_delete_requires_confirmation_and_old_request_cannot_replay(self):
        self.assertEqual(self.client.post(self.url,self.payload(action="delete",tenure_id=self.tenure.pk,confirmed="")).status_code,400)
        self.assertTrue(CadreTenure.objects.filter(pk=self.tenure.pk).exists())
        response=self.client.post(self.url,self.payload(action="delete",tenure_id=self.tenure.pk))
        self.assertEqual(response.status_code,200)
        self.assertFalse(CadreTenure.objects.filter(pk=self.tenure.pk).exists())
        self.assertTrue(User.objects.filter(pk=self.student.pk).exists())
        self.assertEqual(self.client.post(self.url,self.payload(action="delete",tenure_id=self.tenure.pk)).status_code,409)
        self.assertTrue(Audit.objects.filter(action="删除干部履职记录").exists())

    def test_limit_allows_update_but_refuses_record_101(self):
        CadreTenure.objects.bulk_create([CadreTenure(student=self.student,position=f"历史{i}",starts_on=date(2020,1,1)) for i in range(99)])
        self.token=self.client.get(reverse("account_edit",args=[self.student.pk])).context["edit_token"]
        self.assertEqual(self.client.post(self.url,self.payload()).status_code,400)
        self.assertEqual(self.client.post(self.url,self.payload(tenure_id=self.tenure.pk)).status_code,200)
        self.assertEqual(self.student.cadre_tenures.count(),100)

    def test_audit_failure_rolls_back_update_and_delete(self):
        for action in ["save","delete"]:
            with patch("portal.student_views.audit",side_effect=IntegrityError("audit test")),self.assertRaises(IntegrityError):
                self.client.post(self.url,self.payload(action=action,tenure_id=self.tenure.pk))
            self.tenure.refresh_from_db();self.assertEqual(self.tenure.position,"班长")

    def test_incomplete_student_can_manage_tenure_and_returned_html_escapes(self):
        User.objects.filter(pk=self.student.pk).update(id_number="",student_class="")
        self.token=self.client.get(reverse("account_edit",args=[self.student.pk])).context["edit_token"]
        response=self.client.post(self.url,self.payload(position='<script>alert("x")</script>'))
        self.assertEqual(response.status_code,200)
        self.assertNotIn('<script>',response.json()["html"])
        self.assertIn('&lt;script&gt;',response.json()["html"])
        self.student.refresh_from_db();self.assertFalse(self.student.profile_complete)

    def test_csrf_is_required_even_with_a_valid_edit_token(self):
        client=Client(enforce_csrf_checks=True);client.force_login(self.admin)
        self.assertEqual(client.post(self.url,self.payload()).status_code,403)
