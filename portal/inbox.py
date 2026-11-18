"""Per-recipient business messages, written in the caller's business transaction."""
from django.urls import reverse
from .models import InboxNotice, User


def deliver(kind, item, actor, event_key, title, *, to_admins=False):
    if to_admins:
        recipients = User.objects.filter(is_staff=True, is_active=True, is_archived=False).exclude(pk=actor.pk).values_list('pk', flat=True)
    else:
        recipients = [item.student_id] if item.student_id != actor.pk else []
    InboxNotice.objects.bulk_create([InboxNotice(recipient_id=pk, actor=actor, kind=kind, object_id=item.pk,
        event_key=event_key, title=title[:160]) for pk in recipients], ignore_conflicts=True)


def destination(notice):
    return reverse(notice.kind+'_detail', args=[notice.object_id])


def unread_for(user):
    return user.inbox_notices.filter(read_at__isnull=True)


def topic_context(user, kind, object_id):
    cutoff = unread_for(user).filter(kind=kind, object_id=object_id).order_by('-pk').values_list('pk', flat=True).first()
    return {'inbox_topic': {'kind': kind, 'object_id': object_id, 'cutoff': cutoff}} if cutoff else {}
