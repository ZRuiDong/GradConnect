from django.contrib.auth.signals import user_logged_out
from django.dispatch import receiver
from django.utils import timezone
from .models import User
from django.db.models.signals import pre_delete
from django.db.models.deletion import ProtectedError


@receiver(user_logged_out)
def clear_online_status(sender, user, **kwargs):
    if user is not None and not user.is_staff and not user.is_superuser:
        User.objects.filter(pk=user.pk).update(online_until=timezone.now())


@receiver(pre_delete, sender=User)
def protect_super_admin(sender, instance, **kwargs):
    if instance.is_superuser:
        raise ProtectedError('唯一超级管理员不能删除。', [instance])
