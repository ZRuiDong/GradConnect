"""对当前本地QA数据做真实HTTP只读业务检查；登录仅使用QA账号。"""
import argparse
import csv
import http.cookiejar
import io
import json
from html.parser import HTMLParser
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from xml.etree import ElementTree as ET

PROJECT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(PROJECT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE','gradconnect.settings')
import django
django.setup()
from django.conf import settings
from django.utils import timezone
from portal.models import (AttendanceBatch, Conversation, FAQ, GuideAttachment, Leave, Notification,
    Policy, PolicyRevision, Registration, ServiceGuide, StudentImportBatch, StudentRosterBatch, User, Video)
from portal.qa_data import PASSWORD, dataset_report
from portal.leave_reports import csv_text


class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms=[];self.current=None;self.select=None;self.option=None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag == 'form':
            assert self.current is None, '页面不能包含嵌套表单'
            self.current={'attrs':attrs,'inputs':[]};self.forms.append(self.current)
        elif tag == 'input' and self.current is not None and 'disabled' not in attrs:
            self.current['inputs'].append(attrs)
        elif tag == 'select' and self.current is not None:
            self.select={'attrs':attrs,'options':[]}
        elif tag == 'option' and self.select is not None:
            self.select['options'].append(attrs)

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current=None
        elif tag == 'select' and self.current is not None and self.select is not None:
            attrs=self.select['attrs'];options=self.select['options']
            if options and 'name' in attrs and 'disabled' not in attrs:
                selected=next((item for item in options if 'selected' in item),options[0])
                self.current['inputs'].append({'name':attrs['name'],'value':selected.get('value','')})
            self.select=None

    def query(self, class_name):
        form=next(form for form in self.forms if class_name in form['attrs'].get('class','').split())
        assert form['attrs'].get('method') == 'get'
        return [(item['name'],item.get('value','')) for item in form['inputs'] if 'name' in item]


class Client:
    def __init__(self,base):
        self.base=base
        self.cookies=http.cookiejar.CookieJar()
        self.opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self,path,fields=None,headers=None):
        headers=dict(headers or {})
        data=None
        if fields is not None:
            token=next(cookie.value for cookie in self.cookies if cookie.name == settings.CSRF_COOKIE_NAME)
            headers.update({'X-CSRFToken':token,'Referer':self.base+path})
            data=urllib.parse.urlencode(fields).encode()
        request=urllib.request.Request(self.base+urllib.parse.quote(path,safe='/?&=%+'),data=data,headers=headers)
        try:
            response=self.opener.open(request,timeout=30)
        except urllib.error.HTTPError as error:
            response=error
        with response:
            return response.status,urllib.parse.urlsplit(response.url).path,dict(response.headers),response.read()

    def login(self,username,active=True):
        self.request('/login/')
        result=self.request('/login/',{'username':username,'password':PASSWORD})
        assert result[1] == ('/' if active else '/login/'),username


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,default=PROJECT/'docs/QA_DATA_HTTP_RESULTS.json')
    args=parser.parse_args()
    if not settings.DEBUG or not dataset_report().get('exists'):
        parser.error('仅允许在已生成QA数据的本地开发环境运行。')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    checks=[]
    def passed(label):
        checks.append(label);print('PASS',label,flush=True)
    with tempfile.TemporaryFile(mode='w+') as log:
        server=subprocess.Popen([sys.executable,'manage.py','runserver',f'127.0.0.1:{port}','--noreload'],cwd=PROJECT,stdout=log,stderr=log)
        try:
            guest=Client(base)
            until=time.monotonic()+20
            while True:
                try:
                    assert guest.request('/health/')[0] == 200
                    break
                except OSError:
                    if server.poll() is not None or time.monotonic()>until:
                        log.seek(0);raise RuntimeError('本地测试服务启动失败：'+log.read()[-1000:])
                    time.sleep(.1)
            assert guest.request('/accounts/')[1] == '/login/'
            passed('实际本地HTTP服务健康，未登录管理页要求登录')
            admin=Client(base);admin.login('qa_admin')
            other_admin=Client(base);other_admin.login('qa_admin2')
            root=Client(base);root.login('qa_root')
            first=Client(base);first.login('qa000001')
            second=Client(base);second.login('qa000002')
            for name in ['qa000004','qa000005']:
                inactive=Client(base);inactive.login(name,active=False)
            passed('三名普通测试管理员及两名学生可登录，停用/归档学生拒绝登录')
            for client in [admin,first]:
                for path in ['/','/notifications/','/guides/','/leaves/','/internships/','/rentals/','/faqs/','/consultations/','/attendance/','/policies/','/videos/']:
                    assert client.request(path)[0] == 200,path
            assert root.request('/system/portal/user/')[0] == 403
            assert admin.request('/system/portal/user/')[0] == 403
            passed('22个学生与管理页面正常，普通测试管理员不能访问系统账号后台')
            for path in ['/accounts/','/accounts/upload/','/accounts/rosters/graduate/upload/','/accounts/rosters/tenure/upload/','/audits/']:
                assert first.request(path)[0] == 403,path
            passed('学生不能访问学生汇总、导入及操作日志等管理入口')
            for category in Notification.Category.values:
                page=first.request('/notifications/?category='+category+'&page=2')[3].decode()
                assert '第 2 /' in page and '【测试】' in page,category
            for category in ServiceGuide.Category.values:
                assert first.request('/guides/?category='+category+'&page=2')[0] == 200
            passed('所有通知类别与指南类别都能筛选，分类结果具有多页测试数据')
            notice=Notification.objects.filter(created_by__username='qa_admin',status='withdrawn').exclude(attachment='').first()
            assert first.request(f'/notifications/{notice.pk}/')[0] == 404
            assert first.request(f'/notifications/{notice.pk}/file/')[0] == 404
            assert admin.request(f'/notifications/{notice.pk}/')[0] == 200
            passed('已撤下通知及附件学生不可访问，管理员仍可核对')
            seen=set()
            for item in Notification.objects.filter(created_by__username='qa_admin',status='published').exclude(attachment=''):
                extension=Path(item.attachment.name).suffix
                if extension in seen:
                    continue
                response=first.request(f'/notifications/{item.pk}/file/')
                assert response[0] == 200 and response[3],extension
                if extension not in {'.pdf','.png','.jpg','.gif','.webp','.bmp'}:
                    assert response[2]['Content-Disposition'].startswith('attachment;')
                seen.add(extension)
            assert {'.pdf','.doc','.docx','.xls','.xlsx','.png','.jpg','.zip','.txt','.csv'}.issubset(seen)
            passed('13种真实通知附件可下载，Office/文本/ZIP使用下载响应')
            guide=ServiceGuide.objects.filter(created_by__username='qa_admin',status='published').first()
            for attachment in guide.attachments.all():
                assert first.request(f'/guides/{guide.pk}/files/{attachment.pk}/')[0] == 200
            withdrawn=ServiceGuide.objects.filter(created_by__username='qa_admin',status='withdrawn').first()
            assert first.request(f'/guides/{withdrawn.pk}/')[0] == 404
            passed('办事指南多附件可访问，撤下指南隐藏正文与附件')
            own=Conversation.objects.filter(student__username='qa000001').first()
            private=Conversation.objects.filter(student__username='qa000002').first()
            assert first.request(f'/consultations/{own.pk}/')[0] == 200
            assert first.request(f'/consultations/{private.pk}/')[0] == 404
            for category in ['daily','awards','admission','departure','party','campus','academic','other']:
                page=first.request('/faqs/?category='+category)[3].decode()
                assert 'QA-PRIVATE-' not in page
            hidden=FAQ.objects.filter(created_by__username='qa_admin',status='draft').first()
            assert first.request(f'/faqs/{hidden.pk}/')[0] == 404
            passed('私人咨询仅本人可读，八类官方问答无私人口令且草稿不可读')
            for kind in ['internship','rental']:
                own=Registration.objects.filter(student__username='qa000001',kind=kind).first()
                private=Registration.objects.filter(student__username='qa000002',kind=kind).first()
                assert first.request(f'/{kind}s/{own.pk}/')[0] == 200
                assert first.request(f'/{kind}s/{private.pk}/')[0] == 404
                attachment=private.attachments.first()
                assert first.request(f'/{kind}s/{private.pk}/files/{attachment.pk}/')[0] == 404
            passed('实习、租房的私人申请与附件具有学生归属隔离')
            page=admin.request('/accounts/?enrollment_status=graduated&q=qa000003')[3].decode()
            assert 'qa000003' in page.split('<tbody>')[1].split('</tbody>')[0]
            assert 'qa000005' not in admin.request('/accounts/?q=qa000005')[3].decode().split('<tbody>')[1].split('</tbody>')[0]
            assert 'qa000005' in admin.request('/accounts/?q=qa000005&archive=archived')[3].decode().split('<tbody>')[1].split('</tbody>')[0]
            table=admin.request('/accounts/?q=qa000001')[3].decode().split('<tbody>')[1].split('</tbody>')[0]
            assert '测试班长' in table and '测试学习委员' in table and '测试往届委员' not in table and '测试下届委员' not in table
            passed('毕业、归档筛选与多条当前职务显示正确，旧/未来任职不混入当前职务')
            as_of=dataset_report()['as_of']
            query=urllib.parse.urlencode({'month_from':'2024-02','month_to':as_of[:7],'q':'qa000001','day_kind':'total_days','days_min':0,'days_max':31,'sort':'total_days','direction':'desc'})
            page=admin.request('/attendance/?'+query)[3].decode()
            assert '第 1 /' in page and 'qa000001' in page
            assert '0099000012' in admin.request('/attendance/?month_from=&month_to=&q=0099000012')[3].decode()
            table=first.request('/attendance/?year=2026&month=&q=qa000002')[3].decode().split('<tbody>')[1].split('</tbody>')[0]
            assert 'qa000002' not in table
            passed('大量月度记录支持跨年范围、数值排序、前导零学号，学生仍仅查看本人')
            export=admin.request('/leaves/export/?q=qa000019')
            rows=list(csv.DictReader(io.StringIO(export[3].decode('utf-8-sig'))))
            expected_name=csv_text(User.objects.get(username='qa000019').full_name)
            assert export[0] == 200 and rows and all(row['姓名'] == expected_name for row in rows)
            assert first.request('/leaves/export/')[0] == 403
            passed('请假CSV导出姓名与当前测试资料一致，学生不能导出')
            policy=Policy.objects.filter(created_by__username='qa_admin',status='published',revisions__is_draft=True).distinct().first()
            assert 'QA-DRAFT-ONLY' not in first.request(f'/policies/{policy.pk}/')[3].decode()
            for revision in policy.revisions.filter(is_draft=True):
                assert first.request(f'/policy-files/{revision.pk}/')[0] == 404
            passed('制度已有有效版与草稿并存时，草稿正文和附件不泄漏')
            video=Video.objects.filter(uploaded_by__username='qa_admin',is_deleted=False).first()
            response=first.request(f'/videos/{video.pk}/play/',headers={'Range':'bytes=0-31'})
            assert response[0] == 206 and len(response[3]) == 32 and response[2]['Content-Range'].startswith('bytes 0-31/')
            deleted=Video.objects.filter(uploaded_by__username='qa_admin',is_deleted=True).first()
            assert first.request(f'/videos/{deleted.pk}/play/')[0] == 404
            passed('真实H264/AAC短视频支持Range传输，已删除视频拒绝播放')
            for batch in StudentImportBatch.objects.filter(uploaded_by__username='qa_admin'):
                assert admin.request(f'/accounts/imports/{batch.pk}/')[0] == 200
                assert other_admin.request(f'/accounts/imports/{batch.pk}/')[0] == 404
            for batch in StudentRosterBatch.objects.filter(uploaded_by__username='qa_admin'):
                assert admin.request(f'/accounts/roster-previews/{batch.pk}/')[0] == 200
                assert other_admin.request(f'/accounts/roster-previews/{batch.pk}/')[0] == 404
            for batch in AttendanceBatch.objects.filter(uploaded_by__username='qa_admin',is_confirmed=False):
                assert admin.request(f'/attendance/imports/{batch.pk}/')[0] == 200
            passed('预置学生资料、毕业、履职、打卡和旧版预览可读，上传管理员归属生效')
            paths=['/notifications/','/guides/','/leaves/','/internships/','/rentals/','/faqs/','/consultations/','/attendance/','/policies/','/videos/']
            for client in [admin,first]:
                for path in paths + (['/accounts/','/audits/'] if client is admin else []):
                    response=client.request(path+'?per_page=37')
                    assert response[0] == 200,path
                    forms=Forms(response[3].decode())
                    assert ('per_page','37') in forms.query('pagination-jump'),path
                    assert ('page','1') in forms.query('pagination-size'),path
                    assert ('per_page','37') in forms.query('pagination-size'),path
            passed('两端全部22个业务列表都有跳页与每页条数设置，单页或空列表仍可设置')
            classes=list(User.objects.filter(is_staff=False,is_archived=False).exclude(student_class='').order_by('student_class').values_list('student_class',flat=True).distinct()[:2])
            params=[('q','qa'),('per_page','7'),*[('student_class',value) for value in classes]]
            html=admin.request('/accounts/?'+urllib.parse.urlencode(params))[3].decode()
            forms=Forms(html)
            jump=forms.query('pagination-jump')
            assert [value for name,value in jump if name == 'student_class'] == classes
            html=admin.request('/accounts/?'+urllib.parse.urlencode([(name,'2' if name == 'page' else value) for name,value in jump]))[3].decode()
            assert '第 2 /' in html and html.split('<tbody>')[1].split('</tbody>')[0].count('<tr>') == 7
            size=Forms(html).query('pagination-size')
            html=admin.request('/accounts/?'+urllib.parse.urlencode([(name,'20' if name == 'per_page' else value) for name,value in size]))[3].decode()
            assert '第 1 /' in html
            assert ('per_page','20') in Forms(admin.request('/accounts/')[3].decode()).query('pagination-size')
            passed('学生汇总实际跳至第2页，多选班级与关键词保留；改条数回第1页，并记住本模块设置')
            html=admin.request('/attendance/?year=2026&month=&sort=clock_days&direction=asc&per_page=7&page=2')[3].decode()
            jump=Forms(html).query('pagination-jump')
            assert ('month_from','2026-01') in jump and ('month_to','2026-12') in jump
            assert ('sort','clock_days') in jump and ('direction','asc') in jump and ('per_page','7') in jump
            assert html.split('<tbody>')[1].split('</tbody>')[0].count('<tr>') == 7
            sample_student=User.objects.get(username='qa000016')
            html=admin.request('/accounts/?q=qa000016')[3].decode()
            assert 'data-resizable-columns="students"' in html and 'data-reset-columns="students"' in html
            assert html.count('data-column-resizer=') == 8 and 'cell-ellipsis' in html
            assert sample_student.id_number not in html and sample_student.masked_id_number in html
            passed('大量月度数据实际分页保留兼容年月范围与排序；学生表格提供列宽手柄且身份证仍脱敏')
            original_export=admin.request('/leaves/export/?q=qa')[3]
            limited_export=admin.request('/leaves/export/?q=qa&per_page=1&page=2')[3]
            assert original_export == limited_export and len(list(csv.reader(io.StringIO(original_export.decode('utf-8-sig'))))) > 1000
            passed('分页设置不截断请假导出，1500人数据下筛选导出仍包含全部匹配记录')
            own=Conversation.objects.filter(student__username='qa000001').first()
            public=FAQ.objects.filter(status='published',created_by__username='qa_admin').first()
            for client,username,paths in [
                (first,'qa000001',['/faqs/',f'/faqs/{public.pk}/','/consultations/','/consultations/new/',f'/consultations/{own.pk}/']),
                (admin,'qa_admin',['/faqs/',f'/faqs/{public.pk}/','/faqs/new/',f'/faqs/{public.pk}/edit/','/consultations/',f'/consultations/{own.pk}/']),
                (second,'qa000002',[f'/faqs/{public.pk}/'])]:
                user=User.objects.get(username=username)
                for path in paths:
                    response=client.request(path)
                    assert response[0] == 200 and response[2]['Cache-Control'] == 'private, no-store'
                    page=response[3].decode()
                    match=re.search(r"background-image:url\('(data:image/svg\+xml[^']+)'\)",page)
                    assert match,path
                    svg=urllib.parse.unquote(match[1].split(',',1)[1])
                    identity=''.join(ET.fromstring(svg).itertext())
                    assert '姓名：'+user.full_name in identity
                    assert ('账号：' if user.is_staff else '学号：')+username in identity
                    assert page.count('class="support-watermark"') == 1
            passed('咨询/官方问答的列表、详情及新增编辑页均有当前查看者水印，管理员和不同学生身份正确且私有响应不缓存')
            page=first.request('/consultations/')[3].decode()
            notice=re.search(r'<aside class="consultation-guidance"[\s\S]*?</aside>',page).group()
            assert '未找到相关答案后再发起咨询' in notice
            assert '前因后果' not in notice and '继续追问' not in page
            for name in ['/notifications/','/guides/','/policies/','/faqs/']:
                assert f'href="{name}"' in notice
            page=first.request('/consultations/new/')[3].decode()
            assert '提问时，请一次性说明' in page and '前因后果' in page
            assert '未找到相关答案后再发起咨询' not in page and '继续追问' not in page
            replied=Conversation.objects.filter(student__username='qa000001',messages__author__is_staff=True).first()
            unreplied=Conversation.objects.filter(student__username='qa000001').exclude(messages__author__is_staff=True).first()
            assert '发送追问 / 补充' in first.request(f'/consultations/{replied.pk}/')[3].decode()
            page=first.request(f'/consultations/{unreplied.pk}/')[3].decode()
            assert '继续追问' not in page and '发送补充' in page
            passed('列表仅提示先查资料，提问页提示描述前因后果，管理员首次回复后才出现继续追问提示')
            assert 'class="support-watermark"' not in first.request('/notifications/')[3].decode()
            assert guest.request('/faqs/')[1] == '/login/'
            passed('水印仅作用于两个咨询子模块，匿名访问仍要求登录')
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill();server.wait()
    report={'date':timezone.localtime().isoformat(),'passed':len(checks),'checks':checks,'dataset':dataset_report(),
            'business_mutations':False,'qa_login_sessions_created':True,'browser_visual_review':False}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':len(checks),'report':str(args.report)},ensure_ascii=False))


if __name__ == '__main__':
    main()
