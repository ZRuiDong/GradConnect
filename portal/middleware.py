class PrivateResponseMiddleware:
    """避免共享缓存留存学生个人数据；静态资源由 Web 服务器单独处理。"""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path.startswith("/static/"):
            response["Cache-Control"] = "private, no-store"
            response["Referrer-Policy"] = "same-origin"
        return response


class StudentActivityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        from .analytics import speculative_request, touch_activity
        name = getattr(request.resolver_match, "url_name", "")
        if (request.method in {"GET", "POST"} and 200 <= response.status_code < 400
                and name not in {"health", "logout", "activity_ping", "account_presence"}
                and not speculative_request(request)
                and response.get("Content-Type", "").startswith("text/html")):
            touch_activity(request.user)
        return response


class RequirePasswordChangeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def process_view(self, request, view_func, view_args, view_kwargs):
        from django.http import JsonResponse
        from django.shortcuts import redirect
        from django.urls import reverse
        name = getattr(request.resolver_match, 'url_name', '')
        if (request.user.is_authenticated and request.user.password_change_required
                and name not in {'password_change', 'logout', 'health'}):
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', ''):
                return JsonResponse({'error': '请先修改初始密码。', 'redirect': reverse('password_change')}, status=403)
            return redirect('password_change')

    def __call__(self, request):
        return self.get_response(request)

