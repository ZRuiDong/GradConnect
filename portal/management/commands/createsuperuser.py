from django.contrib.auth.management.commands.createsuperuser import Command as CreateSuperUser
from django.contrib.auth import get_user_model
from django.core.management.base import CommandError


class Command(CreateSuperUser):
    def handle(self, *args, **options):
        if get_user_model().objects.using(options.get('database', 'default')).filter(is_superuser=True).exists():
            raise CommandError('已存在唯一超级管理员，不能重复创建。请使用现有账号，忘记密码时可用changepassword命令恢复。')
        result = super().handle(*args, **options)
        get_user_model().objects.using(options.get('database', 'default')).filter(is_superuser=True).update(password_change_required=True)
        return result
