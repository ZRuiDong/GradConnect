bind = "127.0.0.1:8000"
workers = 2
worker_class = "gthread"
threads = 2
timeout = 120
# 保持 gthread：工作进程超时通过心跳判断，不将其误作单个长请求的期限。
# 大批量账号上传的响应等待由 Nginx 的专用 location 设置为 600 秒。
# 自动回收或平滑重启时，允许正在执行的批量校验结束。
graceful_timeout = 600
max_requests = 1000
max_requests_jitter = 100
accesslog = "-"
errorlog = "-"
capture_output = True
# 仅信任同机 Nginx；不要将应用端口直接暴露到公网。
forwarded_allow_ips = "127.0.0.1"
