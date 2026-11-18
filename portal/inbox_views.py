from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from .inbox import destination, unread_for
from .models import InboxNotice
from .pagination import paginated_context


@login_required
@require_GET
def inbox_list(request):
    status = request.GET.get('status', 'unread')
    kind = request.GET.get('kind', '')
    if status not in {'unread', 'read', 'all'} or kind and kind not in InboxNotice.Kind.values:
        return HttpResponseBadRequest('消息筛选条件无效。')
    notices = request.user.inbox_notices.all()
    cutoff = notices.order_by('-pk').values_list('pk', flat=True).first() or 0
    unread_count = unread_for(request.user).count()
    if status != 'all':
        notices = notices.filter(read_at__isnull=status == 'unread')
    if kind:
        notices = notices.filter(kind=kind)
    return render(request, 'portal/inbox.html', {**paginated_context(request, notices), 'unread_count': unread_count,
        'selected_status': status, 'selected_kind': kind, 'kinds': InboxNotice.Kind.choices, 'cutoff': cutoff})


@login_required
@require_GET
def inbox_count(request):
    return JsonResponse({'unread': unread_for(request.user).count()})


@login_required
@require_POST
def inbox_open(request, pk):
    notice = get_object_or_404(request.user.inbox_notices, pk=pk)
    request.user.inbox_notices.filter(kind=notice.kind, object_id=notice.object_id,
        pk__lte=notice.pk, read_at__isnull=True).update(read_at=timezone.now())
    return redirect(destination(notice))


@login_required
@require_POST
def inbox_read_all(request):
    try:
        cutoff = int(request.POST.get('cutoff', ''))
        if not 0 <= cutoff < 2**63:
            raise ValueError
    except (TypeError, ValueError):
        return HttpResponseBadRequest('请刷新消息列表后重试。')
    # Only acknowledge messages already present when this page was rendered.
    unread_for(request.user).filter(pk__lte=cutoff).update(read_at=timezone.now())
    return redirect('inbox_list')


@login_required
@require_POST
def inbox_read_topic(request):
    try:
        kind = request.POST.get('kind', '')
        object_id, cutoff = int(request.POST.get('object_id', '')), int(request.POST.get('cutoff', ''))
        if kind not in InboxNotice.Kind.values or not 0 < object_id < 2**63 or not 0 < cutoff < 2**63:
            raise ValueError
    except (TypeError, ValueError):
        return JsonResponse({'error': '消息参数无效。'}, status=400)
    unread_for(request.user).filter(kind=kind, object_id=object_id, pk__lte=cutoff).update(read_at=timezone.now())
    return JsonResponse({'unread': unread_for(request.user).count()})
