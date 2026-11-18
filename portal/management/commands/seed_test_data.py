import json
from datetime import date
from pathlib import Path
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from portal.qa_data import PASSWORD, clear_dataset, dataset_report, marker, repair_test_names, seed_dataset


class Command(BaseCommand):
    help="仅开发环境：生成覆盖各模块的1500人测试数据；重复执行不覆盖已有演练结果。"

    def add_arguments(self,parser):
        parser.add_argument("--students",type=int,default=1500)
        parser.add_argument("--date",type=date.fromisoformat,help="场景基准日期，默认今天")
        parser.add_argument("--check",action="store_true",help="仅统计本套测试数据")
        parser.add_argument("--clear",action="store_true",help="备份后清理本套标记的数据，保留原有数据")
        parser.add_argument("--repair-names",action="store_true",help="备份后修复旧测试数据中的特殊姓名及姓名快照，保留人工修改")
        parser.add_argument("--report",type=Path,help="将不含密码的数据数量写入JSON")

    def handle(self,*args,**options):
        if not settings.DEBUG:
            raise CommandError("测试数据命令仅允许在DJANGO_DEBUG=1的开发环境执行。")
        if sum(bool(options[key]) for key in ["check", "clear", "repair_names"]) > 1:
            raise CommandError("--check、--clear与--repair-names不能同时使用。")
        if not 60 <= options["students"] <= 5000:
            raise CommandError("--students须在60到5000之间。")
        if options["repair_names"]:
            if marker():
                call_command("backup_data",stdout=self.stdout)
            result=repair_test_names()
            self.stdout.write("旧测试姓名修复完成："+json.dumps(result,ensure_ascii=False))
            return
        if options["clear"]:
            if marker():
                call_command("backup_data",stdout=self.stdout)
            self.stdout.write("已清理本套测试数据。" if clear_dataset() else "没有本套测试数据。")
            return
        if options["check"]:
            report=dataset_report()
        else:
            if not marker():
                call_command("backup_data",stdout=self.stdout)
            report,created=seed_dataset(options["students"],options["date"],progress=lambda value:self.stdout.write(value))
            self.stdout.write(self.style.SUCCESS("测试数据已生成。" if created else "测试数据已存在；没有覆盖已修改的资料或密码。"))
            self.stdout.write(f"普通管理员 qa_admin、qa_admin2、qa_root；学生 qa000001、qa000002。初始密码均为 {PASSWORD}")
        self.stdout.write(json.dumps(report,ensure_ascii=False,indent=2))
        if options["report"]:
            options["report"].parent.mkdir(parents=True,exist_ok=True)
            options["report"].write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
