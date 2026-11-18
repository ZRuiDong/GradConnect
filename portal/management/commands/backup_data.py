import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "备份数据库与上传文件，生成仅当前用户可读的 tar.gz。"

    def add_arguments(self, parser):
        parser.add_argument("--destination", default=str(settings.BASE_DIR / "backups"))

    def handle(self, *args, **options):
        previous_umask = os.umask(0o077)
        target = None
        try:
            destination = Path(options["destination"])
            destination.mkdir(parents=True, exist_ok=True)
            stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S-%f")
            target = destination / f"gradconnect-{stamp}.tar.gz"
            db = settings.DATABASES["default"]
            with tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                if db["ENGINE"].endswith("sqlite3"):
                    with sqlite3.connect(db["NAME"]) as source, sqlite3.connect(work / "database.sqlite3") as copy:
                        source.backup(copy)
                else:
                    if not shutil.which("pg_dump"):
                        raise CommandError("请先安装与 PostgreSQL 服务端兼容的 pg_dump。")
                    env = {**os.environ, "PGHOST": db["HOST"], "PGPORT": str(db["PORT"]), "PGUSER": db["USER"],
                           "PGPASSWORD": db["PASSWORD"], "PGDATABASE": db["NAME"]}
                    names = {'sslmode': 'PGSSLMODE', 'sslrootcert': 'PGSSLROOTCERT',
                             'connect_timeout': 'PGCONNECT_TIMEOUT', 'application_name': 'PGAPPNAME'}
                    for key, name in names.items():
                        if key in db.get('OPTIONS', {}):
                            env[name] = str(db['OPTIONS'][key])
                    result = subprocess.run(["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--file", str(work / "database.dump")],
                                            env=env, capture_output=True, text=True)
                    if result.returncode:
                        raise CommandError("pg_dump 失败，请核对数据库可达性、权限与客户端版本。")
                with tarfile.open(target, "w:gz") as archive:
                    for file in work.iterdir():
                        archive.add(file, arcname=file.name)
                    if settings.MEDIA_ROOT.exists():
                        archive.add(settings.MEDIA_ROOT, arcname="media")
            self.stdout.write(self.style.SUCCESS(f"备份完成：{target}"))
        except Exception:
            if target:
                target.unlink(missing_ok=True)
            raise
        finally:
            os.umask(previous_umask)
