from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods
from django.views.decorators.debug import sensitive_post_parameters
from .models import User
from .services import audit


class OwnPasswordChangeForm(PasswordChangeForm):
    def clean_new_password1(self):
        value = self.cleaned_data['new_password1']
        if self.user.check_password(value):
            from django import forms
            raise forms.ValidationError('新密码不能与当前密码相同，请设置你本人使用的新密码。')
        return value


@login_required
@sensitive_post_parameters('old_password', 'new_password1', 'new_password2')
@require_http_methods(['GET', 'POST'])
def password_change(request):
    changed = None
    with transaction.atomic():
        item = get_object_or_404(User.objects.select_for_update(), pk=request.user.pk, is_active=True, is_archived=False)
        form = OwnPasswordChangeForm(item, request.POST if request.method == 'POST' else None)
        if request.method == 'POST':
            if item.password != request.user.password or item.session_revision != request.user.session_revision:
                form.add_error(None, '账号状态已变化，请重新登录后修改密码。')
            elif form.is_valid():
                changed = form.save(commit=False)
                changed.password_change_required = False
                changed.session_revision += 1
                changed.save(update_fields=['password', 'password_change_required', 'session_revision'])
                audit(changed, '本人修改登录密码', changed)
    if changed:
        request.user = changed
        update_session_auth_hash(request, changed)
        messages.success(request, '密码已修改，其他登录会话已失效。')
        return redirect('password_change_done')
    return render(request, 'registration/password_change.html', {'form': form})
