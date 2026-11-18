from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import AdminPasswordChangeForm
from django.db.models import F
from .models import User


class TemporaryPasswordForm(AdminPasswordChangeForm):
    def save(self, commit=True):
        user = super().save(commit=False)
        user.password_change_required = True
        user.session_revision = F('session_revision')+1
        user.online_until = None
        if commit:
            user.save(update_fields=['password', 'password_change_required', 'session_revision', 'online_until'])
            user.refresh_from_db(fields=['session_revision'])
        return user

@admin.register(User)
class AccountAdmin(UserAdmin):
    change_password_form = TemporaryPasswordForm
    list_display = ("username", "full_name", "department", "is_staff", "is_active")
    search_fields = ("username", "full_name", "department")
    fieldsets = UserAdmin.fieldsets + (("学生信息", {"fields": ("full_name", "department", "id_number", "enrollment_status", "party_branch", "student_class", "political_status", "is_archived")}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("学生信息", {"fields": ("full_name", "department")}),)

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return self.has_module_permission(request) and (obj is None or not obj.is_staff and not obj.is_superuser)

    def get_readonly_fields(self, request, obj=None):
        fields = [*super().get_readonly_fields(request, obj), 'is_superuser']
        if obj is not None and obj.is_superuser:
            fields += ['is_staff', 'is_active', 'is_archived']
        return fields

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions

    def save_model(self, request, obj, form, change):
        if not change:
            obj.password_change_required = True
        if obj.is_archived:
            obj.is_active = False
        if change:
            previous = User.objects.get(pk=obj.pk)
            if (previous.is_active and not obj.is_active) or (obj.is_archived and not previous.is_archived):
                obj.session_revision += 1
                obj.online_until = None
        super().save_model(request, obj, form, change)

admin.site.site_header = "GradConnect 系统管理"
admin.site.site_title = "GradConnect"

