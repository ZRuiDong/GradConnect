from datetime import timedelta
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone
from portal.models import AttendanceBatch, LoginGuard, StudentImportBatch, StudentRosterBatch


class Command(BaseCommand):
    help = "清理过期 Session、超过一天的未确认打卡及账号导入预览和旧登录限流记录。"

    def handle(self, *args, **options):
        call_command("clearsessions")
        cutoff = timezone.now() - timedelta(days=1)
        previews, _ = AttendanceBatch.objects.filter(is_confirmed=False, created_at__lt=cutoff).delete()
        accounts, _ = StudentImportBatch.objects.filter(is_confirmed=False, created_at__lt=cutoff).delete()
        rosters, _ = StudentRosterBatch.objects.filter(is_confirmed=False, created_at__lt=cutoff).delete()
        guards, _ = LoginGuard.objects.filter(updated_at__lt=cutoff).delete()
        self.stdout.write(f"已清理打卡预览 {previews} 条、账号预览 {accounts} 条、名单预览 {rosters} 条、登录限流记录 {guards} 条。")
