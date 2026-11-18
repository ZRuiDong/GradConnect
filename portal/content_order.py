import hashlib
import json
from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from .models import FAQ, Notification, Policy, ServiceGuide, Video
from .services import audit


CONTENT_MODULES = {
    "notification": (Notification, "notification_list", "通知"),
    "guide": (ServiceGuide, "guide_list", "办事指南"),
    "policy": (Policy, "policy_list", "规章制度"),
    "faq": (FAQ, "faq_list", "官方问答"),
    "video": (Video, "video_list", "学习视频"),
}


def order_digest(model):
    rows = list(model.objects.order_by(*model._meta.ordering).values_list("pk", "display_order", "updated_at"))
    return hashlib.sha256(json.dumps(rows, default=str, separators=(",", ":")).encode()).hexdigest()


def content_page(request, queryset, context, kind):
    from .analytics import attach_content_metrics
    attach_content_metrics(context["page_obj"], kind)
    if not request.user.is_staff:
        return context
    model = CONTENT_MODULES[kind][0]
    ids = list(queryset.values_list("pk", flat=True))
    indexes = {pk: index for index, pk in enumerate(ids)}
    digest = order_digest(model)
    for item in context["page_obj"]:
        index = indexes[item.pk]
        moves = [("top", "置顶", ids[0] if index else None, 0),
                 ("up", "上移", ids[index-1] if index else None, index-1),
                 ("down", "下移", ids[index+1] if index+1 < len(ids) else None, index+1),
                 ("bottom", "置底", ids[-1] if index+1 < len(ids) else None, len(ids)-1)]
        payload = {"kind": kind, "pk": item.pk, "actor": request.user.pk, "digest": digest,
                   "moves": {key: [neighbor, position] for key, _, neighbor, position in moves},
                   "per_page": context["per_page"]}
        item.order_controls = {"kind": kind, "position": index+1,
            "token": signing.dumps(payload, salt="content-order", compress=True),
            "buttons": [{"key": key, "label": label, "enabled": neighbor is not None} for key, label, neighbor, _ in moves]}
    context["content_ordering"] = True
    return context


def move_content(request, kind, pk):
    if kind not in CONTENT_MODULES:
        raise Http404
    model, _, label = CONTENT_MODULES[kind]
    action = request.POST.get("move")
    try:
        token = signing.loads(request.POST.get("order_token", ""), salt="content-order", max_age=settings.SESSION_COOKIE_AGE)
        if (not isinstance(token, dict) or token.get("kind") != kind or token.get("pk") != pk or
                token.get("actor") != request.user.pk or action not in {"top", "up", "down", "bottom"}):
            raise signing.BadSignature
    except (signing.BadSignature, ValueError, TypeError):
        raise ValidationError("排序页面已过期或参数无效，请重新核对后调整。")
    with transaction.atomic():
        locked = list(model.objects.select_for_update().order_by("pk"))
        if token.get("digest") != order_digest(model):
            raise ValidationError("展示顺序或内容已变化，请核对刷新后的列表再调整。")
        ids = list(model.objects.order_by(*model._meta.ordering).values_list("pk", flat=True))
        neighbor, position = token["moves"][action]
        if pk not in ids or neighbor not in ids or neighbor == pk:
            raise ValidationError("该内容已处于当前筛选结果的边界，无需移动。")
        ids.remove(pk)
        index = ids.index(neighbor) + (1 if action in {"down", "bottom"} else 0)
        ids.insert(index, pk)
        ranks = {item_id: len(ids)-index for index, item_id in enumerate(ids)}
        for item in locked:
            item.display_order = ranks[item.pk]
        model.objects.bulk_update(locked, ["display_order"], batch_size=200)
        audit(request.user, f"调整{label}展示顺序", next(item for item in locked if item.pk == pk))
    return position // token["per_page"] + 1
