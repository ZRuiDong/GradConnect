# 正式部署与验收

应用已按 Mac 开发、Linux 部署准备。本文件是部署操作说明；需要目标服务器、域名和网络访问条件，不能据此认为已经完成公网部署。当前开发环境未安装 PostgreSQL、Nginx，须在目标机器上完成下面的部署与验收。

## 1. 运行环境

面向约 1500 个学生账号，建议正式运行先采用单台 **4 核 / 8 GB 内存、至少 100 GB SSD** 的 Linux LTS 服务器，应用与数据库同机；2 核 / 4 GB 可作为试运行配置。磁盘须根据视频及备份增长另行扩容。这是部署起点，并非已测得的并发承诺；1500 个账号不等于 1500 人同时访问。沿用 Django 单体、数据库 Session、Nginx、Gunicorn，按实际观测决定是否拆分组件。

使用 Python 3.12 或 3.13、锁定的 `requirements.txt`、PostgreSQL **16 或 17 的当前维护小版本**、与服务端兼容的 `pg_dump`、Nginx、systemd 和 HTTPS 证书。现有 Gunicorn 配置为 2 个进程、每进程 2 个线程，作为初始值；导入、导出与正常访问混合压测后再调整。PostgreSQL 14 已到官方公布的 2026-11-12 维护截止日，不再作为新部署起点；版本生命周期以 [PostgreSQL 官方政策](https://www.postgresql.org/support/versioning/) 为准。项目锁定的 Django 5.2.19 包含 [2026-11-03 官方安全更新](https://www.djangoproject.com/weblog/2026/nov/03/security-releases/)，上线前继续核对安全补丁。

学生可以用电脑或手机浏览器访问。公网部署需要公网可达的域名；校园内网部署的校外访问依赖学校网络或 VPN。正式服务器独立运行，开发用 Mac 关机不影响服务。

## 2. 安装应用与数据库

以 `/srv/gradconnect` 为示例应用目录，建立服务用户 `gradconnect`，所属组 `www-data`。将源码（排除 `.venv`、本地数据库、`.env`、演示媒体、备份和缓存）复制到此目录。应用代码由部署人员维护，运行用户仅需读取代码、写入媒体和备份目录。

以下虚拟环境安装、配置编辑和 Django 管理命令请以 `gradconnect` 用户执行，系统服务配置使用具有 sudo 权限的部署账号执行。确认代码上级目录允许 Nginx 的 `www-data` 组遍历，静态资源和媒体文件允许该组读取。

```sh
sudo install -d -o gradconnect -g www-data -m 0750 /srv/gradconnect/media
sudo install -d -o gradconnect -g www-data -m 0700 /srv/gradconnect/backups
cd /srv/gradconnect
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

在 PostgreSQL 中创建 `gradconnect` 角色和归该角色所有的独立数据库；角色只访问应用数据库，数据库端口不向公网开放。使用数据库管理工具或交互式 `psql` 设置随机密码。

编辑 `.env`，替换密钥、数据库密码和域名；`DATABASE_URL` 中的特殊字符需要 URL 编码。生产环境必须设置 `DJANGO_DEBUG=0` 与 PostgreSQL，程序会拒绝缺少密钥或使用 SQLite 的生产配置。

同机数据库可以使用示例中的回环地址。托管云数据库应使用内网地址，并按云厂商证书要求配置 TLS，例如：

```dotenv
DATABASE_URL='postgresql://gradconnect:URL编码后的密码@数据库域名:5432/gradconnect?sslmode=verify-full&sslrootcert=/srv/gradconnect/certs/root.crt&connect_timeout=5'
```

当前 URL 明确支持 `sslmode`、`sslrootcert`、`connect_timeout`（2–120 秒，默认 5 秒）、`application_name`，不接受未知或重复参数；先前静默忽略的 URL 参数现在会报配置错误。数据库连接最长复用 60 秒，启用连接健康检查；备份命令同步使用上述 TLS 与连接超时选项。证书路径须允许应用及备份服务读取，`verify-full` 的数据库域名须匹配服务端证书。

生成密钥：

```sh
.venv/bin/python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())'
```

手动管理命令执行前，将 `.env` 导出为进程环境（仅加载自己维护、权限受控的配置文件）：

```sh
set -a
. ./.env
set +a
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py check --deploy
```

首次使用正式空数据库时，单独执行 `.venv/bin/python manage.py createsuperuser`，创建唯一超级管理员，再通过“管理员管理”创建业务管理员。已存在超级管理员的升级环境不重复执行创建命令；忘记密码可通过 `changepassword 现有账号名` 交互恢复。普通管理员不能管理管理员账号。不要将本地演示数据库直接投入使用。

## 3. 服务与 HTTPS

1. 按服务器实际路径检查 `deploy/gradconnect.service` 和 `deploy/gunicorn.conf.py`。确认 `gradconnect` 用户能读取 `.env`、应用源码和虚拟环境。
2. 将 `gradconnect.service` 复制到 `/etc/systemd/system/`，执行 `sudo systemctl daemon-reload` 和 `sudo systemctl enable --now gradconnect`。
3. 将 `deploy/nginx.conf` 配置到 Nginx 站点，替换所有域名和路径。必须同时部署 `deploy/proxy_common.conf`，各代理入口通过 `/srv/gradconnect/deploy/proxy_common.conf` 引用它。合并或移除默认站点以避免重复 `default_server`；执行 `sudo nginx -t` 后重载。
4. 将 DNS 解析到服务器，通过受信任的证书服务配置 HTTPS。例如在安装 Certbot 的服务器上执行 `sudo certbot --nginx -d 你的域名`，并验证自动续期。
5. 对外开放 HTTPS（以及签发/续期证书所需的 HTTP），应用 8000 端口和数据库端口仅允许本机访问。

生产中 `USE_X_ACCEL_REDIRECT=1`，Django 先校验登录、数据归属和发布状态，再由 Nginx 内部传送文件，支持视频 Range 请求。`/_protected/` 的 `internal` 必须保留，不能将 `media/` 配成公共静态目录。Nginx 必须覆盖 `X-Forwarded-Proto`，应用仅接受本机代理。

Nginx 的请求体上限按入口设置，并不替代应用的文件校验：普通入口 32 MB，学生账号导入 6 MB，视频上传 512 MB，指南新增/编辑及实习/租房申请（含重新提交入口）256 MB。应用对应限制仍是账号 CSV 5 MB、单视频 500 MB、指南与申请至多 10 个附件且每个 20 MB。变更入口或上传限制时须同步核对代理配置。Nginx 请求体临时目录和系统临时目录应有足够空间，部署压测须包含多附件及大视频上传。

账号批量导入支持单次 2000 人 / 5 MB。同步 `deploy/nginx.conf` 中 `location = /accounts/upload/` 的专用配置（`proxy_read_timeout 600s`），执行 `sudo nginx -t` 后重载。Gunicorn 保持 `gthread`，其 `timeout` 是工作进程心跳超时，不是长请求的响应等待期限。同步 `graceful_timeout=600`，并同步 systemd 的 `KillSignal=SIGTERM`、`TimeoutStopSec=620`，执行 `systemctl daemon-reload` 后在维护窗口重启应用。这样工作进程自动回收及平滑重启可以等待批量请求完成。若服务器前还有其他代理或网关，也要核对该上传入口的等待时间。建议仍在导入结束后部署更新。

生产设置默认启用 HTTPS 跳转、安全 Cookie 和 HSTS。首次配置证书期间应在维护状态操作，证书就绪后再开放学生入口。不要使用开发 `runserver` 承担正式服务。

生产静态资源使用 `ManifestStaticFilesStorage`，CSS/JS 地址带内容哈希。每次部署先执行 `collectstatic --noinput` 再开放新版本，避免旧缓存或缺少 manifest 导致页面样式异常；Nginx 仍按 `/static/` 路径服务 `staticfiles/`。

### 附件、视频与带宽

**当前版本可直接部署的方案是本地私有媒体目录。** 将 `/srv/gradconnect/media` 放在持久化数据盘，应用升级时保留该目录，数据库备份与文件备份同步管理。数据库记录文件引用，文件实体在媒体目录；`staticfiles` 是可重新生成的界面资源，不是上传文件。对 1500 人的信息服务，先按实际文件规模使用此方案即可。

若视频较多、多人同时播放，建议后续接入 OSS 等对象存储，将视频流量与应用服务器分开。例：20 人同时播放、每路码率 2 Mbps，仅视频就需约 40 Mbps，尚未计入其他流量和余量；低带宽云主机可能先遇到视频瓶颈。按实际码率、观看并发和流量预算选择带宽，不能从账号数推导。

当前文件鉴权、Range 与备份实现基于本地文件系统，**尚未实现 OSS 适配，不能只改配置就切换**。接入时应采用私有 Bucket，登录及发布/归属权限核验后签发短期访问地址，并调整上传、下载、视频 Range、撤下内容后的访问期限、历史文件迁移和备份。阿里云的权限机制见 [OSS 官方访问授权说明](https://www.alibabacloud.com/help/en/oss/user-guide/authentication)。不要使用公开 Bucket 暴露咨询或申请附件。

## 4. 备份、清理、日志

安装 `deploy/gradconnect-backup.service`、`deploy/gradconnect-backup.timer`、`deploy/gradconnect-cleanup.service`、`deploy/gradconnect-cleanup.timer` 到 systemd：

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now gradconnect-backup.timer gradconnect-cleanup.timer
sudo systemctl start gradconnect-backup.service
sudo systemctl list-timers
sudo journalctl -u gradconnect --since today
```

默认北京时间 03:00 备份、04:00 清理过期会话、旧限流记录及超过一天的未确认导入预览（打卡、学生账号、毕业名单和履职名单）。`backup_data` 通过环境向 `pg_dump` 传递凭据，不将数据库密码放入进程命令行。备份包含个人信息，必须限制访问并同步到服务器之外；按实际数据规模设置保留策略。

目前每次备份都包含完整数据库与整个媒体目录，不是增量备份，也不会自动删除旧备份。可先采用“每日 7 份、每周 4 份”的异地留存目标，由运维任务在确认新备份和异地复制成功后清理过期副本；本地保留量需结合视频体积与磁盘空间确定。记录备份成功时间、大小和恢复演练结果，不能只确认定时器已启用。建议在低峰备份，并在首次上线与重大升级后至少各完成一次隔离恢复演练。

恢复演练应在隔离的新数据库和新媒体目录进行：解压备份，用 `pg_restore --no-owner --no-acl` 导入 `database.dump` 到新建数据库，将 `media/` 放入演练媒体目录，设置正确权限，然后使用该数据库启动应用并核对记录及附件。SQLite 本地备份使用 `database.sqlite3`。不要未经核对就覆盖正在运行的数据。

配置外部监测定期访问 `https://你的域名/health/`，预期返回 `{"status":"ok"}`；同时监测磁盘、数据库连接、错误日志和备份任务失败。单台机器故障会影响服务，本方案不提供跨服务器自动容灾。

## 5. 上线验收

- 在目标 PostgreSQL 数据库上运行 `python manage.py test portal`（测试账号须有创建测试数据库权限，或在独立测试环境运行），执行 `check --deploy` 和迁移检查。
- 两名学生分别登录，确认只看到本人请假、打卡和私人咨询；直接修改 URL 也不能读取他人数据。
- 核对“通知汇总”位于首页下方，九个类别可分类检索；管理员发布、替换、撤下及重新发布通知后，学生展示与详情/附件访问权限同步变化；使用真实 Word、Excel、PDF、图片等文件验证上传和下载，PDF/图片可在线查看，其他格式下载。更新时将迁移应用至 `0017_notification_academic_research` 或更高版本，并重新收集静态资源。
- 核对“办事指南”位于通知汇总下方，八类筛选及负责人、联系方式、办公地址、正文流程均可查询；Word、Excel、PDF 的多附件上传/下载和撤下权限正确。应用 `0004_serviceguide_guideattachment` 迁移，原在办事项入口应返回 404，历史私人咨询仍可按权限查看。
- 应用 `0005`、`0006` 迁移并重新收集静态资源，核对办事指南下方是“请销假、实习和租房”，再下方为“咨询回复”。历史问答分类和请假信息须保留，历史签字信息不得自动补为同意。
- 分别提交实习及校外租房申请，验证身份快照、日期、其他租房原因、宿舍号的必填规则；用真实 JPG/PNG、Word、Excel、PDF 检查附件下载和他人访问隔离。管理员驳回不填理由应失败，审核通过后重复审核不得覆盖，学生能查看审核意见。
- 咨询与官方问答核对八类组合检索；管理员答复后整理问答，必须先保存草稿再确认发布。其他学生只能查看发布后的整理内容，不能访问原始咨询。
- 完成请假提交、批准、线下销假后线上确认；另测驳回重提和待审核撤销。获批后撤销应被拒绝。
- 按日期范围、状态和关键词组合筛选请假，核对跨天记录和北京时间边界。管理员导出 CSV 应包含所有匹配分页记录及审核/销假信息、姓名学号快照、省市和导师签字选择；学生不能导出，错误日期不能变成无条件导出。
- 应用 `0009`，核对制度八类和视频三类、上传类别必选及学生分类/关键词组合查询；旧分类有映射或归入其他，原内容与文件保留。制度草稿类别不得提前影响学生，发布后才切换；视频修改类别须保持原文件，学生不能编辑，撤销制度或删除视频后分类结果、详情及文件访问均应关闭。
- 发布制度，编辑为草稿，确认原发布版仍可见；撤销后原详情与下载地址不可用。
- 发布、取消问答，完成咨询回复和匿名整理；撤下指南、删除视频后原访问地址不可用。
- 上传真实 PDF、H.264/AAC MP4，用电脑与手机检查预览、播放和拖动；校验 Nginx 上传限制和文件权限。
- 月度打卡应用 `0007` 迁移，确认其位于咨询回复下方且每人每月一行；核对旧正常/迟到/请假记录的汇总及原明细保留。下载五列模板并测试跨月份导入、整数与月长校验、姓名匹配、同人同月更新、分页后整批确认及他人预览隔离。管理员按起止年月、学生及打卡/请假/总天数范围组合筛选，检查跨年、首尾及0天、单边范围；六列上下三角排序应保留条件、数值排序正确且分页稳定，合计覆盖全部筛选记录。学生按年/月只能查本人，错误条件不得变成无条件查询，导入失败时不得部分写入。本轮范围筛选和排序无需新迁移，更新后重新收集静态资源并重启应用。
- 应用 `0008`，确认“学生信息汇总”位于月度打卡下方且学生不可访问。检查旧账号密码/状态/业务记录保留，缺失新字段标为待补全；新建/更新必填规则、政治面貌、列表证件号脱敏及干部履职逐行编辑和当前有效职务正确。
- 更新依赖后应用 `0010`：学生界面由毕业年月改为在校/毕业两类，旧日期保留后台历史，现有学生初始在校；核对状态筛选和新版CSV模板。用真实.xlsx/.xls名单验证批量毕业只修改学籍分类；履职导入默认2999-12-31、可手动调整起止日期，今天覆盖的多条职务全部显示。检查旧/未来任职仅在历史中、预览归属/过期/重复确认/并发冲突以及毕业后原账号和业务记录保留。两份模板为项目内文件，无需Node或Excel客户端运行依赖。
- 使用新九列模板分别验证新增和已有资料更新，更新时初始密码必须留空，密码/权限/启停/归档状态及履职和业务记录不可变化；试导入约1500人的新增/更新，预览分页后仍应处理整批。
- 组合多选筛选后分别勾选处理、选择全部跨页结果，核对名单及当前管理员会话限制；无关联学生删除，有请假/打卡/咨询/履职等记录的学生归档停用并保留历史。测试取消、缺少勾选、CSRF、过期、重放、管理员和越范围 ID 注入，均不能越权删除；归档后旧会话持续失效，恢复后仍需单独启用并重新登录。
- 学生账号批量导入测试必填字段、弱密码、重复或已存在学号、预览后冲突及取消/重复确认；确认账号可登录且仅有学生权限，预览页不显示密码。
- 一次 `migrate` 应用截至 `0015` 的所有迁移，不要只选上面的历史功能迁移。核对通知、制度、问答、指南和视频每次学生详情访问均计数，管理访问不计数；首页按模块汇总，学生只看本人统计，管理员看全体。在线/毕业/停用状态、管理员账号管理、唯一超级管理员及详细报表导出权限正确。
- 在目标机器实际导入约 1500 人，核对预览分页、整批确认、所有账号数量和样本登录；在可接受时间内响应且不中途超时。可用 `.venv/bin/python scripts/smoke_http.py --gunicorn --capacity-rows 1500` 对临时数据库做同类检查。
- 模拟实际查询、提交、审批和视频并发，记录响应时间、错误率、CPU、内存和网络。视频带宽估算为并发观看人数 × 码率，另留余量。
- 完成进程异常自动重启、服务器重启后启动、证书续期及备份恢复演练。

## 6. 更新

先备份，在维护窗口更新代码和锁定依赖，运行迁移、静态资源收集、检查及测试，再 `sudo systemctl restart gradconnect`。涉及数据结构变化时，应结合迁移设计回滚方案，并保持上一版本源码和备份可用。后续可按测量结果引入 Redis、对象存储、CDN 或独立数据库服务器。

2026-11-18 本轮审查没有新增迁移，不改动现有业务数据；应同时更新 Python 源码、Nginx 主配置及新代理片段，并重启服务。正式上线前仍需目标 PostgreSQL 上的事务/并发测试、`nginx -t`、真实证书和媒体转发验收，以及混合业务压测；本地 SQLite 回归不能代替这些验收。部署安全配置也可对照 [Django 官方上线检查表](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/)。

## 7. 站内消息、首次改密与申请纠错更新

本次功能补齐需应用 `0015_inbox_password_registration_corrections`。先备份，再执行 `migrate`、`collectstatic --noinput` 并重启应用；同步Nginx主配置，`/internships/<id>/resubmit/`与`/rentals/<id>/resubmit/`也允许256MB的多附件请求体。保持`portal`在`INSTALLED_APPS`中排在`django.contrib.auth`之前，以启用项目覆盖的`createsuperuser`和`changepassword`命令。

迁移不更改原密码、权限、业务记录或附件引用；只将从未登录且密码可用的历史账号标记为首次登录须改密。新建账号及管理员重置密码后也须先改密；已登录的历史账号不统一强制。原生后台及命令行密码重置均遵循该规则。批量资料更新不会改变密码或改密状态。

消息只向对应学生和启用中的管理员投递，按收件人独立记录已读；新消息与原业务在同一事务写入。升级前历史不补造未读消息，历史待办仍保留在首页。可见页面每60秒轮询个人未读数量，切回页面时也刷新；轮询不会作为主动活跃。消息已读写入使用CSRF保护的POST，详情只确认打开页面前已有的消息。

上线验收增加：新学生/管理员的首次改密、重置后的旧会话失效、改密完成后当前会话保留；两名管理员的独立未读状态；学生咨询追问和申请审核通知；实习/租房撤回与审核同时发生时只能一方成功；驳回重提、沿用/替换附件、原历史保留和重复提交返回现有新申请。真实PostgreSQL下的行锁与唯一约束并发验收仍须在目标环境完成。
