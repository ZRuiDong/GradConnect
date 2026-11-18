"""使用临时数据库验证真实 HTTP 导入与登录；不访问项目现有数据库。"""
import argparse
import base64
import csv
import http.cookiejar
import html
import io
import json
import os
import re
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from zipfile import ZipFile

PROJECT = Path(__file__).resolve().parent.parent
TEST_PASSWORD = "LocalTest!Maple2026"


class BrowserClient:
    """标准库 HTTP 客户端，验证真实 Cookie、CSRF 和重定向。"""
    def __init__(self, base, csrf_name):
        self.base, self.csrf_name = base, csrf_name
        self.personal_passwords = {}
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def send(self, path, data=None, content_type=None, csrf=True, timeout=120):
        path = urllib.parse.quote(path, safe="/?&=%+")
        headers = {"Referer": self.base + path}
        if content_type:
            headers["Content-Type"] = content_type
        if data is not None and csrf:
            token = next(cookie.value for cookie in self.cookies if cookie.name == self.csrf_name)
            headers["X-CSRFToken"] = token
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            response = self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, urllib.parse.urlsplit(response.url).path, response.read()

    def get(self, path):
        return self.send(path)

    def post(self, path, fields, csrf=True):
        return self.send(path, urllib.parse.urlencode(fields, doseq=True).encode(), "application/x-www-form-urlencoded", csrf=csrf)

    def login(self, username):
        assert self.get("/login/")[0] == 200
        password = self.personal_passwords.get(username, TEST_PASSWORD)
        status, path, body = self.post("/login/", {"username": username, "password": password})
        if path == '/password/':
            personal = 'Personal!Cedar9842'
            status, path, body = self.post('/password/', {'old_password': password,
                'new_password1': personal, 'new_password2': personal})
            assert status == 200 and path == '/password/done/', '首次登录修改临时密码失败'
            self.personal_passwords[username] = personal
            status, path, body = self.get('/')
        assert status == 200 and path == "/", "测试账号登录失败"
        return body.decode()

    def upload(self, rows, mode="create"):
        content = io.StringIO(newline="")
        writer = csv.writer(content)
        writer.writerow(["学号", "姓名", "身份证号", "在校状态", "所在党支部", "所在班级", "政治面貌", "院系", "初始密码"])
        writer.writerows([row[0], row[1], "110000199001010001", "在校", "无", "2026级一班", "群众", row[2], row[3]] if len(row) == 4 else row for row in rows)
        return self.multipart("/accounts/upload/", {"mode": mode}, [("file", "students.csv", content.getvalue().encode("utf-8-sig"), "text/csv")], timeout=600)

    def multipart(self, path, fields, files, timeout=120):
        boundary = "GradConnectTest" + secrets.token_hex(12)
        parts = []
        for name, value in fields.items():
            values = value if isinstance(value, (list, tuple)) else [value]
            for item in values:
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{item}\r\n'.encode())
        for field, name, content, content_type in files:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{name}"\r\nContent-Type: {content_type}\r\n\r\n'.encode() + content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        return self.send(path, b"".join(parts), f"multipart/form-data; boundary={boundary}", timeout=timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true", help="验证后保留临时服务器，按回车结束")
    parser.add_argument("--skip-capacity", action="store_true", help="跳过大批量密码哈希耗时测试")
    parser.add_argument("--capacity-rows", type=int, default=1500, help="实测批量人数，默认 1500")
    parser.add_argument("--gunicorn", action="store_true", help="通过部署配置中的 Gunicorn gthread 验证")
    parser.add_argument("--report", type=Path, help="将不含凭据的结果写入 JSON")
    options = parser.parse_args()
    checks = []

    def passed(name):
        checks.append(name)
        print(f"PASS {name}", flush=True)

    with tempfile.TemporaryDirectory(prefix="gradconnect-http-") as temporary:
        work = Path(temporary)
        cookie_suffix = secrets.token_hex(5)
        csrf_name = f"gradconnect_test_csrf_{cookie_suffix}"
        (work / "http_test_settings.py").write_text(
            "from gradconnect.settings import *\n"
            f"DATABASES = {{'default': {{'ENGINE': 'django.db.backends.sqlite3', 'NAME': {str(work / 'isolated.sqlite3')!r}, 'OPTIONS': {{'timeout': 20}}}}}}\n"
            f"MEDIA_ROOT = Path({str(work / 'media')!r})\n"
            f"SESSION_COOKIE_NAME = {'gradconnect_test_session_' + cookie_suffix!r}\n"
            f"CSRF_COOKIE_NAME = {csrf_name!r}\n",
            encoding="utf-8",
        )
        env = {**os.environ, "DJANGO_SETTINGS_MODULE": "http_test_settings", "DJANGO_DEBUG": "1",
               "DJANGO_SECRET_KEY": secrets.token_urlsafe(48), "DJANGO_ALLOWED_HOSTS": "127.0.0.1,localhost",
               "DJANGO_CSRF_TRUSTED_ORIGINS": "", "USE_X_ACCEL_REDIRECT": "0",
               "PYTHONPATH": os.pathsep.join([str(work), str(PROJECT)])}
        env.pop("DATABASE_URL", None)
        os.environ.update(env)
        sys.path[:0] = [str(work), str(PROJECT)]
        import django
        django.setup()
        from django.core.management import call_command
        from django.db import connections
        from django.utils import timezone
        from portal.models import AttendanceBatch, CadreTenure, Conversation, FAQ, Leave, MonthlyAttendance, Notification, Policy, PolicyRevision, Registration, ServiceGuide, StudentImportBatch, User, Video
        from portal.uploads import GUIDE_FILE_TYPES
        from portal.forms import STUDENT_IMPORT_MAX_ROWS
        if not 1 <= options.capacity_rows <= STUDENT_IMPORT_MAX_ROWS:
            parser.error(f"--capacity-rows 必须介于 1 和 {STUDENT_IMPORT_MAX_ROWS} 之间")
        call_command("migrate", verbosity=0)
        for username, staff in [("http-manager", True), ("http-other-manager", True), ("http-student", False), ("http-student-two", False)]:
            User.objects.create_user(username=username, password=TEST_PASSWORD, full_name="测试账号", is_staff=staff)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        with (work / "server.log").open("w+") as log:
            server_command = [sys.executable, "manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"]
            if options.gunicorn:
                server_command = [sys.executable, "-m", "gunicorn", "--config", "deploy/gunicorn.conf.py",
                                  "--bind", f"127.0.0.1:{port}", "gradconnect.wsgi:application"]
            server = subprocess.Popen(server_command, cwd=PROJECT, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                anonymous = BrowserClient(base, csrf_name)
                deadline = time.monotonic() + 15
                while True:
                    try:
                        assert anonymous.get("/health/")[0] == 200
                        break
                    except (OSError, urllib.error.URLError):
                        if time.monotonic() > deadline or server.poll() is not None:
                            raise RuntimeError("临时测试服务启动失败")
                        time.sleep(0.1)
                passed("临时数据库与实际 HTTP 健康检查")
                anonymous.get("/login/")
                for _ in range(6):
                    assert anonymous.post("/login/", {})[0] == 200
                passed("连续空登录请求触发正常校验和限流，不出现服务器错误")
                assert anonymous.get("/accounts/upload/")[1] == "/login/"
                passed("未登录用户不能访问导入入口")
                admin = BrowserClient(base, csrf_name)
                admin.login("http-manager")
                status, _, template = admin.get("/accounts/template/")
                assert status == 200 and template.startswith(b"\xef\xbb\xbf")
                assert list(csv.reader(io.StringIO(template.decode("utf-8-sig")))) == [["学号", "姓名", "身份证号", "在校状态", "所在党支部", "所在班级", "政治面貌", "院系", "初始密码"]]
                passed("管理员登录及 UTF-8 CSV 模板下载")
                status, _, template = admin.get("/accounts/template/?mode=update")
                assert status == 200 and list(csv.reader(io.StringIO(template.decode("utf-8-sig")))) == [["学号", "政治面貌"]]
                assert "下载政治面貌更新模板" in admin.get("/accounts/upload/")[2].decode()
                passed("政治面貌两列更新模板及操作说明可下载查看")

                rows = [["00880001", "HTTP学生甲", "测试学院", TEST_PASSWORD], ["00880002", "HTTP学生乙", "", TEST_PASSWORD]]
                status, preview, body = admin.upload(rows)
                assert status == 200 and preview.startswith("/accounts/imports/")
                assert not User.objects.filter(username="00880001").exists()
                batch = StudentImportBatch.objects.get(pk=int(preview.rstrip("/").split("/")[-1]))
                assert TEST_PASSWORD.encode() not in body and batch.payload[0]["password_hash"].encode() not in body
                passed("真实 multipart 上传与无密码泄露的预览，确认前不创建账号")
                other = BrowserClient(base, csrf_name)
                other.login("http-other-manager")
                assert other.get(preview)[0] == 404
                passed("其他管理员不能访问或确认本人的导入预览")
                assert other.post(preview, {"action": "confirm"})[0] == 404
                assert admin.post(preview, {"action": "confirm"}, csrf=False)[0] == 403
                passed("缺少 CSRF 的确认请求被拒绝")
                assert admin.post(preview, {"action": "confirm"})[:2] == (200, "/accounts/")
                user = User.objects.get(username="00880001")
                assert not user.is_staff and not user.is_superuser and user.check_password(TEST_PASSWORD)
                batch.refresh_from_db()
                assert batch.is_confirmed and batch.payload == []
                passed("确认后原子创建学生账号并清空预览数据")
                student = BrowserClient(base, csrf_name)
                assert "HTTP学生甲" in student.login("00880001")
                assert student.get("/accounts/upload/")[0] == 403
                assert student.get("/accounts/template/")[0] == 403
                passed("新学生真实登录成功，管理入口被拒绝")
                nav_page = student.get("/leaves/")[2].decode()
                applications_group = re.search(r'<details[^>]*data-nav-group="applications"[^>]*>', nav_page).group(0)
                support_group = re.search(r'<details[^>]*data-nav-group="support"[^>]*>', nav_page).group(0)
                assert ' open' in applications_group and ' open' not in support_group
                assert '<summary class="nav-parent' in nav_page and 'aria-controls="nav-applications"' in nav_page
                passed("侧边栏使用原生可折叠分组，当前模块服务端自动展开")
                before = User.objects.count()
                assert "请勿重复确认" in admin.post(preview, {"action": "confirm"})[2].decode()
                assert User.objects.count() == before
                passed("重复确认不会重复创建账号")

                for username, password in [("00880001", TEST_PASSWORD), ("00889999", "123456")]:
                    before = User.objects.count()
                    response = admin.upload([[username, "无效记录", "", password]])
                    assert response[1] == "/accounts/upload/" and "第 2 行" in response[2].decode()
                    assert User.objects.count() == before
                passed("已存在账号和弱密码文件被拒绝")
                _, cancel_url, _ = admin.upload([["00880101", "取消测试", "", TEST_PASSWORD]])
                assert admin.post(cancel_url, {"action": "cancel"})[:2] == (200, "/accounts/")
                assert not User.objects.filter(username="00880101").exists()
                passed("取消导入不创建账号")

                _, conflict_url, _ = admin.upload([["00880201", "冲突测试甲", "", TEST_PASSWORD], ["00880202", "冲突测试乙", "", TEST_PASSWORD]])
                other.get("/accounts/new/")
                result = other.post("/accounts/new/", {"username": "00880202", "full_name": "另行创建", "id_number": "110000199001010001", "enrollment_status": "enrolled", "party_branch": "", "student_class": "2026级一班", "department": "", "password1": TEST_PASSWORD, "password2": TEST_PASSWORD})
                assert result[0] == 200 and result[1] == f"/accounts/{User.objects.get(username='00880202').pk}/"
                assert User.objects.get(username="00880202").profile_complete
                passed("党支部留空可创建学生，资料完整性不受影响")
                assert "本次未导入任何账号" in admin.post(conflict_url, {"action": "confirm"})[2].decode()
                assert not User.objects.filter(username="00880201").exists()
                passed("预览后另一管理员创建同名账号，确认时整批拒绝")

                student.get("/leaves/new/")
                fields = {"kind": "personal", "starts_at": "2026-11-10T08:00", "ends_at": "2026-11-11T17:00",
                          "reason": "HTTP范围内请假", "province": "浙江省", "city": "杭州市", "tutor_signature": "no", "contact": "013800000000"}
                assert student.post("/leaves/new/", fields)[0] == 200
                item = Leave.objects.get(reason="HTTP范围内请假")
                outside = {**fields, "starts_at": "2026-10-01T08:00", "ends_at": "2026-10-02T08:00", "reason": "HTTP范围外请假"}
                assert student.post("/leaves/new/", outside)[0] == 200
                passed("真实 HTTP 提交日期范围内外两笔请假")
                query = urllib.parse.urlencode({"start_date": "2026-11-10", "end_date": "2026-11-12", "q": "00880001"})
                status, _, page = admin.get("/leaves/?" + query)
                assert status == 200 and f"/leaves/{item.pk}/".encode() in page
                outside_item = Leave.objects.get(reason="HTTP范围外请假")
                assert f"/leaves/{outside_item.pk}/".encode() not in page
                passed("日期范围与学号组合筛选只返回匹配请假")
                assert admin.post(f"/leaves/{item.pk}/actions/approve/", {"note": "HTTP审核通过"})[0] == 200
                assert admin.post(f"/leaves/{item.pk}/actions/close/", {"note": "HTTP线下销假已确认"})[0] == 200
                status, _, exported = admin.get("/leaves/export/?" + query + "&status=closed")
                assert status == 200 and exported.startswith(b"\xef\xbb\xbf")
                records = list(csv.DictReader(io.StringIO(exported.decode("utf-8-sig"))))
                assert len(records) == 1 and records[0]["请假原因"] == "HTTP范围内请假"
                assert records[0]["开始时间"] == "2026-11-10 08:00:00" and records[0]["学号"] == "00880001"
                assert records[0]["审核意见"] == "HTTP审核通过" and records[0]["销假说明"] == "HTTP线下销假已确认"
                passed("真实 CSV 下载包含筛选后的申请、审核与销假信息")
                assert student.get("/leaves/export/")[0] == 403
                assert admin.get("/leaves/export/?start_date=2026-11-12&end_date=2026-11-01")[0] == 400
                passed("学生不能导出，错误日期不会退回全部数据")

                page = admin.get("/notifications/")
                assert page[0] == 200 and "发布通知" in page[2].decode()
                fields = {"title": "HTTP招聘信息通知", "category": "recruitment", "summary": "岗位说明", "body": "HTTP岗位申请安排"}
                response = admin.post("/notifications/new/", fields)
                assert response[0] == 200
                notice = Notification.objects.get(title=fields["title"])
                assert response[1] == f"/notifications/{notice.pk}/"
                passed("管理员通过真实 HTTP 在招聘信息类别发布通知")
                search = "/notifications/?" + urllib.parse.urlencode({"category": "recruitment", "q": "岗位申请"})
                response = student.get(search)
                assert response[0] == 200 and fields["title"] in response[2].decode()
                assert "HTTP岗位申请安排" in student.get(f"/notifications/{notice.pk}/")[2].decode()
                assert student.get("/notifications/new/")[0] == 403
                assert student.post(f"/notifications/{notice.pk}/actions/withdraw/", {})[0] == 403
                passed("学生可分类检索通知并查看正文，但不能发布或撤下")
                response = admin.post(f"/notifications/{notice.pk}/edit/", {**fields, "title": "HTTP竞赛通知", "category": "competition", "body": "更新后的竞赛安排"})
                assert response[0] == 200
                assert "更新后的竞赛安排" in student.get(f"/notifications/{notice.pk}/")[2].decode()
                notice.refresh_from_db()
                assert notice.category == "competition"
                assert "HTTP竞赛通知" not in student.get(search)[2].decode()
                passed("通知替换内容和类别后学生看到最新结果")
                assert admin.post(f"/notifications/{notice.pk}/actions/withdraw/", {})[0] == 200
                assert student.get(f"/notifications/{notice.pk}/")[0] == 404
                assert student.get(f"/notifications/{notice.pk}/file/")[0] == 404
                assert admin.post(f"/notifications/{notice.pk}/actions/publish/", {})[0] == 200
                assert student.get(f"/notifications/{notice.pk}/")[0] == 200
                passed("撤下通知后学生不能访问原地址，重新发布后恢复")

                notification_fields = {"title": "HTTP多格式附件通知", "category": "other", "body": ""}
                office_files = {}
                for extension in [".doc", ".xls"]:
                    office_files[extension] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(504)
                for extension, main_part in [(".docx", "word/document.xml"), (".xlsx", "xl/workbook.xml")]:
                    output = io.BytesIO()
                    with ZipFile(output, "w") as package:
                        package.writestr("[Content_Types].xml", "<Types/>")
                        package.writestr(main_part, "<document/>")
                    office_files[extension] = output.getvalue()
                office_files[".pdf"] = b"%PDF-1.4 notification HTTP test"
                office_files[".png"] = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6ZAAAAABJRU5ErkJggg==")
                for extension, content in office_files.items():
                    response = admin.multipart("/notifications/new/", {**notification_fields, "title": notification_fields["title"] + extension},
                        [("attachment", "通知附件" + extension, content, "application/octet-stream")])
                    assert response[0] == 200 and response[1].startswith("/notifications/") and response[1] != "/notifications/new/"
                    attachment_url = response[1] + "file/"
                    detail = student.get(response[1])[2].decode()
                    assert "下载附件" in detail
                    assert ('class="notification-image-preview"' in detail) == (extension == ".png")
                    assert ('class="pdf-preview"' in detail) == (extension == ".pdf")
                    assert student.get(attachment_url)[2] == content
                    assert student.get(attachment_url + "?download=1")[2] == content
                passed("通知支持Word、Excel、PDF和PNG真实上传及原字节下载，按格式显示预览入口")
                attachment_notice_url = response[1]
                assert admin.post(attachment_notice_url + "actions/withdraw/", {})[0] == 200
                assert student.get(attachment_notice_url + "file/")[0] == 404
                response = admin.multipart(attachment_notice_url + "edit/", {**notification_fields, "body": "已替换为Word"},
                    [("attachment", "替换附件.docx", office_files[".docx"], "application/octet-stream")])
                assert response[0] == 200
                assert student.get(attachment_notice_url + "file/")[0] == 404
                assert admin.post(attachment_notice_url + "actions/publish/", {})[0] == 200
                assert student.get(attachment_notice_url + "file/")[2] == office_files[".docx"]
                assert 'class="notification-image-preview"' not in student.get(attachment_notice_url)[2].decode()
                passed("图片通知撤下、撤下时换成Word并重新发布后，文件访问和页面展示同步更新")
                before_notices = Notification.objects.count()
                response = admin.multipart("/notifications/new/", notification_fields,
                    [("attachment", "伪装附件.png", b"not an image", "image/png")])
                assert response[1] == "/notifications/new/" and "图片内容与所选格式不符" in response[2].decode()
                assert Notification.objects.count() == before_notices
                passed("通知拒绝内容伪装的图片，未创建通知或保存附件")

                fields = {"title": "HTTP学生证办理指南", "category": "daily", "owner": "HTTP王老师",
                          "contact": "12345678", "office": "事务中心101", "process": "1.准备材料。\n2.提交办理。"}
                files = [("attachments", "guide.pdf", b"%PDF-1.4 HTTP test", "application/pdf")]
                for extension, main_part in [(".docx", "word/document.xml"), (".xlsx", "xl/workbook.xml")]:
                    output = io.BytesIO()
                    with ZipFile(output, "w") as package:
                        package.writestr("[Content_Types].xml", "<Types/>")
                        package.writestr(main_part, "<document/>")
                    files.append(("attachments", "guide" + extension, output.getvalue(), GUIDE_FILE_TYPES[extension]))
                response = admin.multipart("/guides/new/", fields, files)
                assert response[0] == 200
                guide = ServiceGuide.objects.get(title=fields["title"])
                assert guide.attachments.count() == 3
                passed("管理员真实发布办事指南并同时上传 Word、Excel 和 PDF 附件")
                query = urllib.parse.urlencode({"category": "daily", "q": "HTTP王老师"})
                page = student.get("/guides/?" + query)
                assert page[0] == 200 and fields["title"] in page[2].decode()
                detail = student.get(f"/guides/{guide.pk}/")[2].decode()
                assert all(value in detail for value in ["HTTP王老师", "12345678", "事务中心101", "准备材料"])
                assert student.get("/guides/new/")[0] == 403
                assert admin.get("/matters/")[0] == 404 and student.get("/matters/")[0] == 404
                nav = student.get("/")[2].decode().split('<nav aria-label="主导航">', 1)[1].split("</nav>", 1)[0]
                assert nav.index('/notifications/') < nav.index('/guides/') < nav.index('/policies/')
                assert "在办事项" not in nav
                passed("学生可分类检索指南，导航顺序正确，旧在办事项入口已停用")
                expected = {name: content for _, name, content, _ in files}
                file_urls = []
                for attachment in guide.attachments.all():
                    url = f"/guides/{guide.pk}/files/{attachment.pk}/"
                    status, _, downloaded = student.get(url)
                    assert status == 200 and downloaded == expected[attachment.original_name]
                    file_urls.append(url)
                passed("学生下载 Word、Excel 和 PDF 附件内容与上传内容一致")
                assert admin.post(f"/guides/{guide.pk}/actions/withdraw/", {})[0] == 200
                assert student.get(f"/guides/{guide.pk}/")[0] == 404
                assert all(student.get(url)[0] == 404 for url in file_urls)
                assert admin.post(f"/guides/{guide.pk}/edit/", {**fields, "category": "academic", "process": "新的办理步骤"})[0] == 200
                assert student.get(f"/guides/{guide.pk}/")[0] == 404
                assert admin.post(f"/guides/{guide.pk}/actions/publish/", {})[0] == 200
                assert "新的办理步骤" in student.get(f"/guides/{guide.pk}/")[2].decode()
                assert student.get(file_urls[0])[0] == 200
                passed("撤下指南后正文及全部附件被拒绝，修改并重新发布后恢复")
                if not options.gunicorn:
                    page = student.get("/guides/")[2].decode()
                    css_path = page.split('rel="stylesheet" href="', 1)[1].split('"', 1)[0]
                    status, _, css = student.get(css_path)
                    assert status == 200 and "?v=" in css_path and b".category-filters{display:grid" in css
                    assert 'class="category-filters"' in page
                    passed("学生端分类使用换行网格，带版本号的新样式文件实际可访问")

                second_student = BrowserClient(base, csrf_name)
                second_student.login("http-student-two")
                fields = {"starts_at": "2026-11-12T09:00", "ends_at": "2026-12-12T18:00", "employer": "HTTP实习研究院",
                          "position": "科研实习", "address": "浙江省杭州市西湖区示例路1号2栋301室", "contact": "13800000000",
                          "emergency_name": "HTTP紧急联系人", "emergency_contact": "13900000000"}
                file = ("attachments", "application.png", b"\x89PNG\r\n\x1a\nHTTP test", "image/png")
                response = student.multipart("/internships/new/", fields, [file])
                assert response[0] == 200
                internship = Registration.objects.get(kind="internship")
                assert internship.status == "pending" and internship.student_number == "00880001"
                assert fields["employer"] in admin.get("/internships/?q=HTTP&status=pending&start_date=2026-11-20")[2].decode()
                passed("学生真实上传图片并提交实习申请，管理员可按时间与状态查询")
                file_url = f"/internships/{internship.pk}/files/{internship.attachments.get().pk}/"
                assert student.get(file_url)[2] == file[2]
                assert second_student.get(file_url)[0] == 404
                assert second_student.get(f"/internships/{internship.pk}/")[0] == 404
                assert student.post(f"/internships/{internship.pk}/review/", {"action": "approve"})[0] == 403
                assert admin.post(f"/internships/{internship.pk}/review/", {"action": "approve", "note": "同意实习"})[0] == 200
                assert "同意实习" in student.get(f"/internships/{internship.pk}/")[2].decode()
                passed("实习附件原字节下载、学生隔离和管理员审核通过")
                rental_fields = {**fields, "rental_reason": "other", "reason_detail": "HTTP家庭安排", "has_dorm": "yes", "dorm_number": "东区2栋201"}
                response = student.multipart("/rentals/new/", rental_fields, [("attachments", "rental.pdf", b"%PDF-1.4 test", "application/pdf")])
                assert response[0] == 200
                rental = Registration.objects.get(kind="rental")
                assert rental.has_dorm and rental.dorm_number == "东区2栋201" and rental.reason_detail == "HTTP家庭安排"
                assert admin.post(f"/rentals/{rental.pk}/review/", {"action": "reject"})[0] == 400
                assert admin.post(f"/rentals/{rental.pk}/review/", {"action": "reject", "note": "请补充导师签字"})[0] == 200
                assert "请补充导师签字" in student.get(f"/rentals/{rental.pk}/")[2].decode()
                assert second_student.get(f"/rentals/{rental.pk}/")[0] == 404
                passed("校外租房保存其他理由与宿舍号，驳回必须写原因且学生能查看")
                response = admin.get("/leaves/export/?start_date=2026-11-10&end_date=2026-11-11")
                rows = list(csv.DictReader(io.StringIO(response[2].decode("utf-8-sig"))))
                assert rows[0]["去往城市"] == "杭州市" and rows[0]["是否导师本人线下签字"] == "否：使用电子签名或代签，已征得导师同意"
                passed("按日期实际导出包含省市、身份快照和导师签字选择")
                assert student.post("/consultations/new/", {"category": "academic", "subject": "HTTP私人主题", "body": "HTTP私人身份信息"})[0] == 200
                consultation = Conversation.objects.get(subject="HTTP私人主题")
                assert "HTTP私人主题" in student.get("/consultations/?category=academic&q=私人")[2].decode()
                assert "HTTP私人主题" not in second_student.get("/consultations/?category=academic")[2].decode()
                assert second_student.get(f"/consultations/{consultation.pk}/")[0] == 404
                assert admin.post(f"/consultations/{consultation.pk}/", {"body": "HTTP管理员答复"})[0] == 200
                passed("分类私人咨询、本人检索和管理员一对一回复")
                response = admin.get(f"/faqs/new/?source={consultation.pk}")
                assert 'value="academic" selected' in response[2].decode()
                assert "HTTP管理员答复" in response[2].decode() and "HTTP私人身份信息" not in response[2].decode()
                assert admin.post(f"/faqs/new/?source={consultation.pk}", {"category": "academic", "question": "HTTP公开通用问题", "answer": "HTTP整理后的公开答案"})[0] == 200
                faq = FAQ.objects.get(source=consultation)
                assert faq.status == "draft" and second_student.get(f"/faqs/{faq.pk}/")[0] == 404
                assert admin.post(f"/faqs/{faq.pk}/actions/publish/", {})[0] == 200
                response = second_student.get("/faqs/?category=academic&q=通用")
                assert "HTTP公开通用问题" in response[2].decode()
                response = second_student.get(f"/faqs/{faq.pk}/")[2].decode()
                assert "HTTP整理后的公开答案" in response and "HTTP私人主题" not in response and "HTTP私人身份信息" not in response
                assert second_student.get(f"/consultations/{consultation.pk}/")[0] == 404
                passed("咨询整理带入分类并确认发布到官方问答，原始会话持续私密")

                response = admin.get("/attendance/template/")
                assert response[0] == 200 and response[2].startswith(b"\xef\xbb\xbf")
                assert list(csv.reader(io.StringIO(response[2].decode("utf-8-sig")))) == [["学号", "姓名", "月份", "打卡记录", "请假天数"]]
                page = student.get("/attendance/")[2].decode()
                nav = page.split('<nav aria-label="主导航">')[1].split('</nav>')[0]
                assert nav.index('/consultations/') < nav.index('/attendance/') < nav.index('/policies/')
                assert "每日记录" not in page and "打卡状态" not in page
                passed("月度打卡位于咨询回复下方，五列月度导入模板可实际下载")
                content = "学号,姓名,月份,打卡记录,请假天数\n00880001,HTTP学生甲,2026-11,20,2\n00880001,HTTP学生甲,2026-10,18,1\nhttp-student-two,测试账号,2026-11,10,3\n"
                response = admin.multipart("/attendance/upload/", {}, [("file", "monthly.csv", content.encode("utf-8-sig"), "text/csv")])
                assert response[0] == 200 and response[1].startswith("/attendance/imports/")
                attendance_preview = response[1]
                assert "HTTP学生甲" in response[2].decode() and "2026-10" in response[2].decode()
                assert MonthlyAttendance.objects.count() == 0
                assert other.get(attendance_preview)[0] == 404
                assert admin.post(attendance_preview, {"action": "confirm"}, csrf=False)[0] == 403
                assert admin.post(attendance_preview, {"action": "confirm"})[0] == 200
                assert MonthlyAttendance.objects.count() == 3
                batch = AttendanceBatch.objects.get(pk=int(attendance_preview.rstrip("/").split("/")[-1]))
                assert batch.is_confirmed and batch.payload == [] and batch.month is None
                passed("跨月份 CSV 真实上传预览并整批确认，预览归属与 CSRF 生效")
                page = student.get("/attendance/?year=2026&month=11&q=http-student-two&student=http-student-two")[2].decode()
                assert "HTTP学生甲" in page and "http-student-two" not in page and "22 天" in page
                assert "2026年10月" not in page
                page = admin.get("/attendance/?year=2026&month=10&q=00880001")[2].decode()
                assert "2026年10月" in page and "19 天" in page and "http-student-two" not in page
                page = second_student.get("/attendance/?year=2026&month=11")[2].decode()
                assert "13 天" in page and "00880001" not in page and "HTTP学生甲" not in page
                passed("按年、月、学生查询汇总，学生只能看到本人及本人的总天数")
                page = admin.get("/attendance/?month_from=2025-12&month_to=2026-11&q=00880001")[2].decode()
                table = page.split('<tbody>')[1].split('</tbody>')[0]
                assert table.count('<tr>') == 2 and "2026年10月" in table and "2026年11月" in table
                assert "http-student-two" not in table and "2 条月度记录（1 名学生）" in page
                assert 'name="month_from" value="2025-12"' in page and 'name="month_to" value="2026-11"' in page
                passed("管理员年月区间支持跨年及含首尾筛选，并与学生关键词组合")
                for category, lower, upper, count, has_other in [("clock_days", 18, 20, 2, False), ("leave_days", 3, 3, 1, True), ("total_days", 19, 22, 2, False)]:
                    query = urllib.parse.urlencode({"month_from": "2026-10", "month_to": "2026-11", "day_kind": category, "days_min": lower, "days_max": upper})
                    table = admin.get("/attendance/?" + query)[2].decode().split('<tbody>')[1].split('</tbody>')[0]
                    assert table.count('<tr>') == count and ("http-student-two" in table) == has_other
                passed("管理员可按打卡、请假或总天数的闭区间筛选每条月度记录")
                page = admin.get("/attendance/?month_from=2026-10&month_to=2026-11")[2].decode()
                assert page.count('class="sort-triangle asc"') == 6 and page.count('class="sort-triangle desc"') == 6
                order_cases = {"学号": ([22, 19, 13], [13, 22, 19]), "姓名": ([22, 19, 13], [13, 22, 19]),
                               "月份": ([19, 22, 13], [22, 13, 19]), "打卡天数": ([13, 19, 22], [22, 19, 13]),
                               "请假天数": ([19, 22, 13], [13, 22, 19]), "总天数": ([13, 19, 22], [22, 19, 13])}
                for label, directions in order_cases.items():
                    for text, expected in zip(["升序", "降序"], directions):
                        link = re.search(r'href="([^"]+)" aria-label="按' + label + text + '排列"', page).group(1)
                        target_url = "/attendance/" + html.unescape(link)
                        sorted_page = admin.get(target_url)[2].decode()
                        table = sorted_page.split('<tbody>')[1].split('</tbody>')[0]
                        assert [int(value) for value in re.findall(r'<strong>([0-9]+) 天</strong>', table)] == expected
                        sort_params = urllib.parse.parse_qs(urllib.parse.urlsplit(target_url).query)
                        assert sort_params.get("month_from") == ["2026-10"] and "page" not in sort_params
                passed("六列表头上下三角的12个排序链接均生效，保留范围并按真实数值排序")
                page = admin.get("/attendance/?month_from=2026-12&month_to=2026-11&sort=total_days&direction=desc")[2].decode()
                assert "结束年月不能早于起始年月" in page and "所选条件下暂无月度打卡记录" in page
                page = student.get("/attendance/?year=2026&month=11&month_from=&month_to=&days_min=0&days_max=1&sort=total_days&direction=asc")[2].decode()
                table = page.split('<tbody>')[1].split('</tbody>')[0]
                assert "22 天" in table and "http-student-two" not in table
                passed("错误范围不会返回未筛选数据，排序与伪造管理员参数不能绕过学生隔离")
                changed = "学号,姓名,月份,打卡记录,请假天数\n00880001,HTTP学生甲,2026-11,21,3\n"
                response = admin.multipart("/attendance/upload/", {}, [("file", "monthly.csv", changed.encode("utf-8-sig"), "text/csv")])
                assert admin.post(response[1], {"action": "confirm"})[0] == 200
                assert MonthlyAttendance.objects.count() == 3
                assert MonthlyAttendance.objects.get(student_number="00880001", month="2026-11-01").total_days == 24
                assert MonthlyAttendance.objects.get(student_number="00880001", month="2026-10-01").total_days == 19
                assert admin.post(response[1], {"action": "confirm"})[0] == 200
                assert MonthlyAttendance.objects.count() == 3
                passed("同人同月重导更新且保留其他月份，重复确认不重复创建")
                before = AttendanceBatch.objects.count()
                invalid = "学号,姓名,月份,打卡记录,请假天数\n00880001,错误姓名,2026-11,20,2\n"
                response = admin.multipart("/attendance/upload/", {}, [("file", "monthly.csv", invalid.encode("utf-8-sig"), "text/csv")])
                assert "姓名为空或与该学号的学生姓名不一致" in response[2].decode()
                assert AttendanceBatch.objects.count() == before and MonthlyAttendance.objects.count() == 3
                assert student.get("/attendance/template/")[0] == 403
                assert student.get("/attendance/upload/")[0] == 403
                passed("错误姓名整批拒绝，学生没有模板下载和导入权限")

                target = User.objects.get(username="00880001")
                original_password = target.password
                response = admin.upload([[target.username, target.full_name, "110000199001010001", "在校", "无", "批量补全班级", "群众", target.department, ""]], mode="update")
                assert response[0] == 200 and "确认更新 1 名学生资料" in response[2].decode()
                assert admin.post(response[1], {"action": "confirm"})[:2] == (200, "/accounts/")
                target.refresh_from_db()
                assert target.student_class == "批量补全班级" and target.password == original_password and target.is_active and not target.is_staff
                passed("已有学生可批量补全资料，原密码、权限和登录状态保持不变")
                before = User.objects.values().get(pk=target.pk)
                partial_csv = f"学号,政治面貌\n{target.username},预备党员\n"
                response = admin.multipart("/accounts/upload/", {"mode": "update"}, [("file", "partial.csv", partial_csv.encode("utf-8-sig"), "text/csv")])
                assert "本次仅更新：<strong>政治面貌</strong>" in response[2].decode()
                assert User.objects.values().get(pk=target.pk) == before
                assert admin.post(response[1], {"action": "confirm"})[:2] == (200, "/accounts/")
                assert User.objects.values().get(pk=target.pk) == {**before, "political_status": "probationary"}
                passed("两列 CSV 预览后仅更新政治面貌，其他全部学生字段保留")
                clear_csv = f"学号,所在党支部\n{target.username},\n"
                response = admin.multipart("/accounts/upload/", {"mode": "update"}, [("file", "clear.csv", clear_csv.encode("utf-8-sig"), "text/csv")])
                assert admin.post(response[1], {"action": "confirm"})[:2] == (200, "/accounts/")
                target.refresh_from_db()
                assert target.party_branch == "" and target.profile_complete and target.political_status == "probationary"
                filtered = admin.get("/accounts/?party_branch=__empty__&completeness=complete")[2].decode().split('<tbody>')[1].split('</tbody>')[0]
                assert target.username in filtered
                passed("选填党支部可批量清空，并可按未填写和资料完整性组合查询")
                profile = {"full_name": target.full_name, "id_number": target.id_number, "enrollment_status": "enrolled",
                           "party_branch": "HTTP党支部", "student_class": "HTTP班级", "political_status": "activist",
                           "department": target.department, "is_active": "on", "tenures-TOTAL_FORMS": "2", "tenures-INITIAL_FORMS": "0",
                           "tenures-MIN_NUM_FORMS": "0", "tenures-MAX_NUM_FORMS": "100",
                           "tenures-0-position": "HTTP旧职务", "tenures-0-starts_on": "2025-09-01", "tenures-0-ends_on": "2026-08-31",
                           "tenures-1-position": "HTTP当前职务", "tenures-1-starts_on": "2026-09-01", "tenures-1-ends_on": ""}
                edit_page = admin.get(f"/accounts/{target.pk}/edit/")[2].decode()
                profile["edit_token"] = re.search(r'name="edit_token" value="([^"]+)"', edit_page).group(1)
                response = admin.post(f"/accounts/{target.pk}/edit/", profile)
                assert response[:2] == (200, f"/accounts/{target.pk}/")
                assert CadreTenure.objects.filter(student=target).count() == 2
                assert "HTTP旧职务" in response[2].decode() and "HTTP当前职务" in response[2].decode()
                query = urllib.parse.urlencode({"student_class": "HTTP班级", "party_branch": "HTTP党支部", "political_status": "activist",
                                                "enrollment_status": "enrolled", "current_position": "HTTP当前职务"})
                response = admin.get("/accounts/?" + query)
                table = response[2].decode().split('<tbody>')[1].split('</tbody>')[0]
                assert "HTTP当前职务" in table and "HTTP旧职务" not in table and "00880002" not in table
                assert target.id_number not in response[2].decode()
                passed("真实维护多条干部履职历史，列表仅展示当前职务并支持多类别组合筛选")
                combined = admin.get("/accounts/?" + urllib.parse.urlencode({"q": "HTTP班级 HTTP当前职务"}))
                combined_table = combined[2].decode().split('<tbody>')[1].split('</tbody>')[0]
                assert "00880001" in combined_table and "00880002" not in combined_table
                assert combined[2].decode().count('data-filter-dropdown data-label=') == 4
                assert "Command" not in combined[2].decode() and "data-option-search" in combined[2].decode()
                passed("综合搜索跨字段组合匹配，筛选页提供四个可搜索的复选下拉控件")
                nav = response[2].decode().split('<nav aria-label="主导航">')[1].split('</nav>')[0]
                assert nav.index('/attendance/') < nav.index('/accounts/') < nav.index('/policies/')
                assert "学生信息汇总" in nav
                assert student.get(f"/accounts/{target.pk}/")[0] == 403
                passed("学生信息汇总位于月度打卡下方，完整身份证仅管理详情可见")
                stale_edit_page = admin.get(f"/accounts/{target.pk}/edit/")[2].decode()
                stale_token = re.search(r'name="edit_token" value="([^"]+)"', stale_edit_page).group(1)
                stale_csv = f"学号,政治面貌\n{target.username},群众\n"
                stale_preview = admin.multipart("/accounts/upload/", {"mode": "update"}, [("file", "partial.csv", stale_csv.encode("utf-8-sig"), "text/csv")])[1]
                fresh_update = other.upload([[target.username, target.full_name, target.id_number, "在校", "HTTP党支部", "HTTP班级（已由其他管理员核对）", "共青团员（入党积极分子）", target.department, ""]], mode="update")[1]
                assert other.post(fresh_update, {"action": "confirm"})[:2] == (200, "/accounts/")
                stale_profile = {key: value for key, value in profile.items() if not key.startswith("tenures-")}
                stale_profile["edit_token"] = stale_token
                assert admin.post(f"/accounts/{target.pk}/edit/", stale_profile)[0] == 409
                assert "本次未更新任何资料" in admin.post(stale_preview, {"action": "confirm"})[2].decode()
                target.refresh_from_db()
                assert target.student_class == "HTTP班级（已由其他管理员核对）"
                assert CadreTenure.objects.filter(student=target).count() == 2
                assert admin.post(stale_preview, {"action": "cancel"})[:2] == (200, "/accounts/")
                passed("单人编辑与批量更新预览检测并发冲突，不覆盖其他管理员的新资料")
                stale_delete = admin.post("/accounts/delete/prepare/", {"ids": [target.pk]})[1]
                latest_update = other.upload([[target.username, target.full_name, target.id_number, "在校", "HTTP党支部", "HTTP班级（删除核对后再次调整）", "共青团员（入党积极分子）", target.department, ""]], mode="update")[1]
                assert other.post(latest_update, {"action": "confirm"})[:2] == (200, "/accounts/")
                assert admin.post(stale_delete, {"confirmed": "on"})[0] == 400
                assert User.objects.filter(pk=target.pk, is_archived=False, is_active=True).exists()
                assert admin.post(stale_delete, {"action": "cancel"})[:2] == (200, "/accounts/")
                passed("删除核对后班级等筛选资料变化时阻止执行，须重新核对名单")
                unlinked = User.objects.get(username="00880002")
                response = admin.post("/accounts/delete/prepare/", {"ids": [target.pk, unlinked.pk], "selection_mode": "selected"})
                delete_preview = response[1]
                assert response[0] == 200 and delete_preview.startswith("/accounts/delete/previews/")
                assert "彻底删除 1 名" in response[2].decode() and "归档并停用 1 名" in response[2].decode()
                assert other.get(delete_preview)[0] == 404
                assert admin.post(delete_preview, {"confirmed": ""})[0] == 400
                assert User.objects.filter(pk=unlinked.pk).exists()
                assert admin.post(delete_preview, {"confirmed": "on"})[:2] == (200, "/accounts/")
                target.refresh_from_db()
                assert target.is_archived and not target.is_active
                assert not User.objects.filter(pk=unlinked.pk).exists()
                assert CadreTenure.objects.filter(student=target).count() == 2
                assert Conversation.objects.filter(pk=consultation.pk, student=target).exists()
                assert MonthlyAttendance.objects.filter(student=target).count() == 2
                assert student.get("/")[1] == "/login/"
                passed("真实核对后批量删除：无关联学生彻底删除，有关联学生归档停用并保留所有历史")
                assert admin.post(delete_preview, {"confirmed": "on"})[0] == 404
                response = admin.get("/accounts/?archive=archived&q=00880001")
                assert "00880001" in response[2].decode().split('<tbody>')[1]
                assert admin.post(f"/accounts/{target.pk}/status/restore/", {})[0] == 200
                target.refresh_from_db()
                assert not target.is_archived and not target.is_active
                assert admin.post(f"/accounts/{target.pk}/status/enable/", {})[0] == 200
                assert student.get("/")[1] == "/login/"
                assert "HTTP学生甲" in student.login("00880001")
                passed("归档学生可恢复并单独启用，旧会话持续失效且原密码可重新登录")

                from portal.test_rosters import excel_fixture
                from django.utils import timezone
                from datetime import timedelta
                import openpyxl
                for kind, headers in [("graduate", ["学号", "姓名"]), ("tenure", ["学号", "姓名", "任职职务", "开始日期", "结束日期"])]:
                    response = admin.get(f"/accounts/rosters/{kind}/template/")
                    assert response[0] == 200
                    book = openpyxl.load_workbook(io.BytesIO(response[2]), read_only=True)
                    assert list(next(book.worksheets[0].iter_rows(values_only=True))) == headers
                    assert book.worksheets[0]["A1501"].number_format == "@"
                    book.close()
                    assert student.get(f"/accounts/rosters/{kind}/upload/")[0] == 403
                    assert student.get(f"/accounts/rosters/{kind}/template/")[0] == 403
                passed("毕业名单与履职Excel模板可下载并保留1500行文本学号格式，学生无管理权限")
                target.refresh_from_db()
                before = User.objects.values().get(pk=target.pk)
                upload = excel_fixture("graduate", [[target.username, target.full_name]])
                response = admin.multipart("/accounts/rosters/graduate/upload/", {}, [("file", upload.name, upload.read(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")])
                graduation_preview = response[1]
                assert graduation_preview.startswith("/accounts/roster-previews/") and "在校 → 毕业" in response[2].decode()
                assert User.objects.values().get(pk=target.pk) == before
                assert other.get(graduation_preview)[0] == 404
                assert admin.post(graduation_preview, {"action": "confirm"}, csrf=False)[0] == 403
                assert admin.post(graduation_preview, {"action": "confirm"})[:2] == (200, "/accounts/")
                assert User.objects.values().get(pk=target.pk) == {**before, "enrollment_status": "graduated"}
                assert "请勿重复确认" in admin.post(graduation_preview, {"action": "confirm"})[2].decode()
                assert target.username in admin.get("/accounts/?enrollment_status=graduated")[2].decode().split('<tbody>')[1]
                passed("Excel毕业名单预览确认仅修改在校状态，保留原密码登录及业务记录并支持毕业筛选")
                today = timezone.localdate()
                rows = [[target.username, target.full_name, role, start, ""] for role, start in [
                    ("HTTP双职务甲", today.isoformat()), ("HTTP双职务乙", today.isoformat()),
                    ("HTTP未来职务", (today + timedelta(days=1)).isoformat())]]
                upload = excel_fixture("tenure", rows)
                response = admin.multipart("/accounts/rosters/tenure/upload/", {}, [("file", upload.name, upload.read(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")])
                assert response[1].startswith("/accounts/roster-previews/") and "2999-12-31" in response[2].decode()
                assert admin.post(response[1], {"action": "confirm"})[:2] == (200, "/accounts/")
                query = urllib.parse.urlencode({"q": target.username, "current_position": ["HTTP双职务甲", "HTTP双职务乙"]}, doseq=True)
                table = admin.get("/accounts/?" + query)[2].decode().split('<tbody>')[1].split('</tbody>')[0]
                assert table.count('<tr>') == 1 and "HTTP双职务甲" in table and "HTTP双职务乙" in table and "HTTP未来职务" not in table
                passed("Excel履职导入默认2999年结束，多条当前职务同时显示，未来职务仅保留历史")
                upload = excel_fixture("tenure", [[target.username, target.full_name, "不应写入的职务", today.isoformat(), ""]])
                stale_preview = admin.multipart("/accounts/rosters/tenure/upload/", {}, [("file", upload.name, upload.read(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")])[1]
                edit = admin.get(f"/accounts/{target.pk}/edit/")[2].decode()
                target.refresh_from_db()
                role_rows = list(target.cadre_tenures.all())
                profile = {"full_name": target.full_name, "id_number": target.id_number, "enrollment_status": target.enrollment_status,
                    "party_branch": target.party_branch, "student_class": target.student_class, "political_status": target.political_status,
                    "department": target.department, "is_active": "on", "edit_token": re.search(r'name="edit_token" value="([^"]+)"', edit).group(1),
                    "tenures-TOTAL_FORMS": str(len(role_rows)), "tenures-INITIAL_FORMS": str(len(role_rows)), "tenures-MAX_NUM_FORMS": "100"}
                for index, role in enumerate(role_rows):
                    start = today - timedelta(days=2) if role.position == "HTTP双职务甲" else role.starts_on
                    end = today - timedelta(days=1) if role.position == "HTTP双职务甲" else role.ends_on
                    profile.update({f"tenures-{index}-id": str(role.pk), f"tenures-{index}-position": role.position,
                        f"tenures-{index}-starts_on": start.isoformat(), f"tenures-{index}-ends_on": end.isoformat() if end else ""})
                assert admin.post(f"/accounts/{target.pk}/edit/", profile)[:2] == (200, f"/accounts/{target.pk}/")
                table = admin.get("/accounts/?q=" + target.username)[2].decode().split('<tbody>')[1].split('</tbody>')[0]
                assert "HTTP双职务甲" not in table and "HTTP双职务乙" in table
                assert "整批未修改" in admin.post(stale_preview, {"action": "confirm"})[2].decode()
                assert not CadreTenure.objects.filter(student=target, position="不应写入的职务").exists()
                passed("手动修改履职日期立即更新当前职务，旧履职导入预览检测变更并整批拒绝")
                upload = excel_fixture("graduate", [[target.username, "错误姓名"]])
                response = admin.multipart("/accounts/rosters/graduate/upload/", {}, [("file", upload.name, upload.read(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")])
                assert response[1] == "/accounts/rosters/graduate/upload/" and "姓名与账号不一致" in response[2].decode()
                response = admin.multipart("/accounts/rosters/graduate/upload/", {}, [("file", "broken.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(504), "application/vnd.ms-excel")])
                assert response[0] == 200 and "无法读取Excel" in response[2].decode()
                passed("毕业名单姓名与学号不一致时拒绝创建预览或修改学生")

                fields = {"title": "HTTP学生纪律制度", "category": "discipline", "summary": "校园纪律要求", "body": "HTTP纪律制度正文"}
                pdf = b"%PDF-1.4 category test"
                response = admin.multipart("/policies/new/", fields, [("attachment", "discipline.pdf", pdf, "application/pdf")])
                assert response[0] == 200
                policy = Policy.objects.get(revisions__title=fields["title"])
                revision = policy.revisions.get()
                assert revision.category == "discipline" and revision.is_draft
                assert student.get(f"/policies/{policy.pk}/")[0] == 404
                assert admin.post(f"/policies/{policy.pk}/actions/publish/", {})[0] == 200
                response = student.get("/policies/?" + urllib.parse.urlencode({"category": "discipline", "q": "纪律要求"}))
                assert f'/policies/{policy.pk}/' in response[2].decode()
                assert student.get(f"/policy-files/{revision.pk}/")[2] == pdf
                passed("规章制度选择校纪校规上传并确认发布，学生可分类检索及下载PDF")
                assert admin.post(f"/policies/{policy.pk}/edit/", {**fields, "title": "HTTP党建制度新版本", "category": "party", "body": "新版本党建要求"})[0] == 200
                assert f'/policies/{policy.pk}/' in student.get("/policies/?category=discipline")[2].decode()
                assert f'/policies/{policy.pk}/' not in student.get("/policies/?category=party")[2].decode()
                assert admin.post(f"/policies/{policy.pk}/actions/publish/", {})[0] == 200
                assert f'/policies/{policy.pk}/' in student.get("/policies/?category=party")[2].decode()
                assert f'/policies/{policy.pk}/' not in student.get("/policies/?category=discipline")[2].decode()
                assert admin.post(f"/policies/{policy.pk}/actions/withdraw/", {})[0] == 200
                assert student.get(f"/policies/{policy.pk}/")[0] == 404
                assert student.get(f"/policy-files/{revision.pk}/")[0] == 404
                passed("制度草稿改分类保持原发布分类，确认发布后切换，撤销后不可访问")
                movie = b"\x00\x00\x00\x18ftypisom" + b"0" * 100
                fields = {"title": "HTTP班团学习视频", "category": "class_group", "description": "班团活动介绍"}
                assert admin.multipart("/videos/new/", fields, [("file", "course.mp4", movie, "video/mp4")])[0] == 200
                video = Video.objects.get(title=fields["title"])
                original_file = video.file.name
                assert f'/videos/{video.pk}/' in student.get("/videos/?" + urllib.parse.urlencode({"category": "class_group", "q": "活动介绍"}))[2].decode()
                assert student.get(f"/videos/{video.pk}/play/")[2] == movie
                assert admin.post(f"/videos/{video.pk}/edit/", {**fields, "category": "party"})[0] == 200
                video.refresh_from_db()
                assert video.file.name == original_file and video.category == "party"
                assert f'/videos/{video.pk}/' in student.get("/videos/?category=party")[2].decode()
                assert f'/videos/{video.pk}/' not in student.get("/videos/?category=class_group")[2].decode()
                passed("视频按班团分类上传和检索，管理员可修改类别且保持原视频文件")
                assert student.post(f"/videos/{video.pk}/edit/", fields)[0] == 403
                assert student.post(f"/policies/{policy.pk}/edit/", {"category": "other"})[0] == 403
                assert admin.post(f"/videos/{video.pk}/delete/", {})[0] == 200
                assert student.get(f"/videos/{video.pk}/")[0] == 404
                assert student.get(f"/videos/{video.pk}/play/")[0] == 404
                assert f'/videos/{video.pk}/' not in student.get("/videos/?category=party")[2].decode()
                passed("学生无权修改制度和视频类别，已删除视频不再出现在分类或播放入口")

                capacity = None
                if not options.skip_capacity:
                    row_count = options.capacity_rows
                    capacity_rows = [[str(90000000 + index), f"批量测试{index + 1}", "测试学院", TEST_PASSWORD] for index in range(row_count)]
                    print(f"RUN {row_count} 人真实密码哈希与整批创建检查…", flush=True)
                    start = time.perf_counter()
                    status, capacity_url, _ = admin.upload(capacity_rows)
                    upload_seconds = time.perf_counter() - start
                    assert status == 200 and capacity_url.startswith("/accounts/imports/")
                    if row_count > 100:
                        status, _, second_page = admin.get(capacity_url + "?page=2")
                        assert status == 200 and capacity_rows[100][0].encode() in second_page
                        assert capacity_rows[0][0].encode() not in second_page
                    start = time.perf_counter()
                    assert admin.post(capacity_url, {"action": "confirm"})[:2] == (200, "/accounts/")
                    confirm_seconds = time.perf_counter() - start
                    imported = User.objects.filter(username__in=[row[0] for row in capacity_rows])
                    assert imported.count() == row_count and not imported.filter(is_staff=True).exists()
                    assert len(set(imported.values_list("password", flat=True))) == row_count
                    capacity = {"rows": row_count, "upload_and_validate_seconds": round(upload_seconds, 3),
                                "confirm_seconds": round(confirm_seconds, 3)}
                    passed(f"{row_count} 人完整导入，使用真实 PBKDF2 且每人盐值不同")
                report = {"date": timezone.localtime().isoformat(), "environment": "macOS / local SQLite / real HTTP / default PBKDF2",
                          "server": "Gunicorn gthread" if options.gunicorn else "Django development server",
                          "passed": len(checks), "checks": checks,
                          "capacity": capacity,
                          "existing_project_data_modified": False}
                if options.report:
                    options.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
                if options.preview:
                    print(f"PREVIEW_URL={base}/login/", flush=True)
                    input("测试服务仅使用临时数据库；浏览器检查后按回车结束：")
            finally:
                server.send_signal(signal.SIGQUIT if options.gunicorn else signal.SIGTERM)
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
                connections.close_all()


if __name__ == "__main__":
    main()
