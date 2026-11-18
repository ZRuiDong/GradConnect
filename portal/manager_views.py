from functools import wraps
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm, SetPasswordForm
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import F
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from .models import User
from .services import audit
from .views import form_page, page_context


def superuser_only(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser or not request.user.is_staff or not request.user.is_active:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped


class ManagerCreationForm(UserCreationForm):
    class Meta:
        model = User
        fields = ['username', 'full_name', 'email', 'department']
        labels = {'username': '管理员登录账号', 'full_name': '姓名', 'email': '电子邮箱（选填）', 'department': '所属单位（选填）'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['full_name'].required = True

    def save(self, commit=True):
        item = super().save(commit=False)
        item.is_staff, item.is_superuser, item.is_active, item.is_archived = True, False, True, False
        item.password_change_required = True
        if commit:
            item.save()
        return item


class ManagerProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['full_name', 'email', 'department']
        labels = {'full_name': '姓名', 'email': '电子邮箱（选填）', 'department': '所属单位（选填）'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['full_name'].required = True


@superuser_only
def manager_list(request):
    records = User.objects.filter(is_staff=True, is_superuser=False).order_by('-is_active', 'username', 'pk')
    q = request.GET.get('q', '').strip()[:200]
    total = records.count()
    active = records.filter(is_active=True).count()
    if q:
        from django.db.models import Q
        records = records.filter(Q(username__icontains=q) | Q(full_name__icontains=q) | Q(department__icontains=q) | Q(email__icontains=q))
    return render(request, 'portal/manager_list.html', {**page_context(request, records), 'q': q,
        'manager_total': total, 'manager_active': active, 'manager_disabled': total-active,
        'student_total': User.objects.filter(is_staff=False, is_superuser=False, is_archived=False).count()})


@superuser_only
def manager_create(request):
    form = ManagerCreationForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            item = form.save()
            audit(request.user, '创建普通管理员', item)
        messages.success(request, '管理员账号已创建，首次登录须先设置自己的新密码，再处理业务。')
        return redirect('manager_list')
    return form_page(request, form, '创建管理员', reverse('manager_list'), '新账号首次登录须修改临时密码，随后具有全部日常业务管理权限。超级管理员始终仅有一个。', '创建管理员')


@superuser_only
def manager_edit(request, pk):
    with transaction.atomic():
        item = get_object_or_404(User.objects.select_for_update(), pk=pk, is_staff=True, is_superuser=False)
        form = ManagerProfileForm(request.POST or None, instance=item)
        if request.method == 'POST' and form.is_valid():
            form.save(commit=False).save(update_fields=['full_name', 'email', 'department'])
            audit(request.user, '修改管理员资料', item)
            messages.success(request, '管理员资料已更新。')
            return redirect('manager_list')
    return form_page(request, form, '修改管理员 · '+item.username, reverse('manager_list'), '账号登录名保持不变。', '保存修改')


@superuser_only
def manager_reset(request, pk):
    with transaction.atomic():
        item = get_object_or_404(User.objects.select_for_update(), pk=pk, is_staff=True, is_superuser=False)
        form = SetPasswordForm(item, request.POST or None)
        if request.method == 'POST' and form.is_valid():
            manager = form.save(commit=False)
            manager.password_change_required = True
            manager.save(update_fields=['password', 'password_change_required'])
            User.objects.filter(pk=manager.pk).update(session_revision=F('session_revision')+1)
            audit(request.user, '重置管理员密码', item)
            messages.success(request, '管理员临时密码已重置，原登录会话已失效；下次登录须先设置自己的新密码。')
            return redirect('manager_list')
    return form_page(request, form, '重置管理员密码 · '+item.username, reverse('manager_list'), '重置后对方需使用新密码重新登录。', '重置密码')


@superuser_only
@require_POST
def manager_action(request, pk, action):
    if action not in {'enable', 'disable', 'logout'}:
        raise Http404
    with transaction.atomic():
        item = get_object_or_404(User.objects.select_for_update(), pk=pk, is_staff=True, is_superuser=False)
        item.session_revision += 1
        fields = ['session_revision']
        if action != 'logout':
            item.is_active = action == 'enable'
            item.is_archived = False
            fields += ['is_active', 'is_archived']
        item.save(update_fields=fields)
        label = {'enable': '启用管理员', 'disable': '停用管理员', 'logout': '强制管理员退出'}[action]
        audit(request.user, label, item)
    messages.success(request, label+'已完成。历史业务记录保留。')
    return redirect('manager_list')
