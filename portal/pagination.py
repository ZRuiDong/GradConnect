from django.core.paginator import Paginator


def _page_size(value, default):
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if 1 <= number <= 100 else default


def paginated_context(request, queryset, per_page=12, params=None):
    """Keep filters and a bounded, per-list page-size preference across requests."""
    session = getattr(request, "session", None)
    key = getattr(getattr(request, "resolver_match", None), "url_name", None) or request.path
    stored = session.get("list_page_sizes", {}) if session is not None else {}
    if not isinstance(stored, dict):
        stored = {}
    default = _page_size(stored.get(key), per_page)
    selected = _page_size(request.GET.get("per_page"), default)
    if session is not None and "per_page" in request.GET and stored.get(key, per_page) != selected:
        sizes = dict(stored)
        if len(sizes) >= 30 and key not in sizes:
            sizes.pop(next(iter(sizes)))
        sizes[key] = selected
        session["list_page_sizes"] = sizes
    query = (params if params is not None else request.GET).copy()
    query.pop("page", None)
    query["per_page"] = str(selected)
    paginator = Paginator(queryset, selected)
    try:
        number = max(1, int(request.GET.get("page", 1)))
    except (TypeError, ValueError, OverflowError):
        number = 1
    page = paginator.get_page(number)
    links = []
    for value in paginator.get_elided_page_range(page.number, on_each_side=1, on_ends=1):
        if value == paginator.ELLIPSIS:
            links.append({"ellipsis": True})
        else:
            target = query.copy()
            target["page"] = value
            links.append({"number": value, "url": "?" + target.urlencode(), "current": value == page.number})
    filters = [(name, value) for name, values in query.lists() if name != "per_page" for value in values]
    sizes = sorted({10, 12, 20, 50, 100, selected})
    return {"page_obj": page, "querystring": query.urlencode(), "per_page": selected, "page_size_options": sizes,
            "page_links": links, "pagination_filters": filters, "q": request.GET.get("q", "").strip()[:200]}
