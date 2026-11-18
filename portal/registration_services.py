from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from .inbox import deliver
from .models import Registration, RegistrationAttachment, RegistrationEvent
from .services import audit
from .uploads import rollback_uploads


def save_registration(form, actor, kind, source=None):
    with rollback_uploads() as uploads, transaction.atomic():
        retained = []
        if source is not None:
            source = Registration.objects.select_for_update().get(pk=source.pk, student=actor, kind=kind)
            if source.status not in {'withdrawn', 'rejected'} or Registration.objects.filter(previous=source).exists():
                raise ValidationError('原申请状态已变化或已经重新提交，请返回详情核对。')
            selected = set(form.cleaned_data.get('retain_attachments', []))
            retained = list(source.attachments.filter(pk__in=selected))
            if len(retained) != len(selected) or any(not item.file.storage.exists(item.file.name) for item in retained):
                raise ValidationError('选中的原附件已不可用，请重新上传申请附件。')
        item = form.save(commit=False)
        item.student, item.kind = actor, kind
        item.student_name = form.cleaned_data['account_name']
        item.student_number = form.cleaned_data['account_number']
        item.previous = source
        item.save()
        for original in retained:
            RegistrationAttachment.objects.create(registration=item, original_name=original.original_name, file=original.file.name)
        for upload in form.cleaned_data['attachments']:
            uploads.save(RegistrationAttachment(registration=item, original_name=upload.name, file=upload))
        title = Registration.Kind(kind).label
        RegistrationEvent.objects.create(registration=item, actor=actor, status='pending',
            note=f'修改原申请 #{source.pk} 后重新提交' if source else '提交申请')
        audit(actor, f'重新提交{title}' if source else f'提交{title}', item)
        deliver(kind, item, actor, f'{kind}:{item.pk}:pending', f'{actor.display_name}提交{title} #{item.pk}', to_admins=True)
        return item


@transaction.atomic
def withdraw_registration(pk, actor, kind):
    item = Registration.objects.get(pk=pk, student=actor, kind=kind)
    changed = Registration.objects.filter(pk=pk, student=actor, status='pending').update(status='withdrawn', updated_at=timezone.now())
    if not changed:
        raise ValidationError('只有待审核申请可以撤回；状态已变化，请刷新后查看。')
    RegistrationEvent.objects.create(registration=item, actor=actor, status='withdrawn', note='学生撤回，原申请与附件保留。')
    audit(actor, f'撤回{Registration.Kind(kind).label}', item)
    deliver(kind, item, actor, f'{kind}:{item.pk}:withdrawn',
        f'{actor.display_name}撤回{Registration.Kind(kind).label} #{item.pk}', to_admins=True)
    return item
