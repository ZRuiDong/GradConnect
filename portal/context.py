from django.conf import settings
from .watermarks import support_watermark


def navigation(request):
    name = request.resolver_match.url_name if request.resolver_match else ""
    active = name.split("_")[0] if name else "dashboard"
    subsection = "faq" if active == "support" else active
    if active in {"faq", "consultation", "support"}:
        active = "support"
    if active in {"leave", "internship", "rental", "applications"}:
        subsection = "leave" if active == "applications" else active
        active = "applications"
    version = ""
    if settings.DEBUG:
        version = str(max((settings.BASE_DIR / "static" / name).stat().st_mtime_ns for name in ["app.css", "app.js"]))
    watermark = support_watermark(request.user) if active == "support" and request.user.is_authenticated else None
    unread = (request.user.inbox_notices.filter(read_at__isnull=True).count()
        if request.user.is_authenticated and not request.user.password_change_required else 0)
    return {"active_nav": active, "active_subnav": subsection, "debug_asset_version": version,
            "support_watermark": watermark, "inbox_unread_count": unread}

