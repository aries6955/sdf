# 水电费管理

单用户本地 Web 应用,管理房东总表与租户分表的抄表、用量和费用。

## 运行

```bash
cp password.example.txt password.txt          # 编辑其中的登录密码
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

浏览器访问 http://127.0.0.1:8000 。改密码请编辑 `password.txt` 并重启服务。

数据保存在项目目录的 `data.sqlite3`,由单进程独占写入。

## 密码存在哪

三层,优先级从高到低:

1. `/data/password.txt`(宿主 `/root/sdf-data/password.txt`)—— **只要存在就用它**。
   在页面「设置 → 登录密码」改密码会写这个文件,立即生效,不用重启。
2. `SDF_PASSWORD` 环境变量 —— 仅当上面那个文件不存在时生效。
3. 都没有则用 `admin123`。

登录成功后不再回读 `SDF_PASSWORD`。所以改完密码后容器里那个环境变量就发布时间了 ——
忘了密码时,删掉 `/root/sdf-data/password.txt` 并重启容器,就回到 `SDF_PASSWORD`。

## Docker 部署

镜像由 GitHub Actions 推到 `ghcr.io/aries6955/sdf:latest`,推 `main` 即重建。
服务器上不需要克隆源码,直接跑:

```bash
docker run -d --name sdf --restart unless-stopped \
  -p 8536:8536 \
  -e SDF_PASSWORD='你的密码' \
  -v /root/sdf-data:/data \
  ghcr.io/aries6955/sdf:latest
```

访问 `http://<服务器IP>:8536`。`SDF_PASSWORD` 不设则用 `admin123`,登录后到「设置」改。
数据落在宿主 `/root/sdf-data`(容器内的 `/data`),删容器重建不丢。
换目录就改 `-v` 左边那一段,容器内始终是 `/data`。
想改密码/换备份密钥而不重建容器,用下面的 `.env` 方式。

仓库里的 `docker-compose.yml` 是等价写法,想用 compose 才需要拷这一个文件:

```bash
docker compose up -d                      # 默认挂 /root/sdf-data
SDF_DATA=/opt/sdf docker compose up -d    # 换到别的目录
```

容器以 root 运行,所以宿主目录不需要提前 `chown`。

## 异地备份(Cloudflare R2)

应用内的「备份」写在数据卷的 `backups/` 里,和数据库同盘 —— 整机或卷丢了会一起没。
异地靠宿主 cron 每天推一份一致快照到 R2。rclone 已随镜像发布,服务器上不用另装;
宿主只需一个三行的转发脚本,凭据写在宿主的 `.env` 里,改密码或换密钥都不必重建容器。

准备(只做一次):

1. Cloudflare 控制台建一个 R2 桶,比如 `sdf-backup`(保持私有)。
   R2 → Manage R2 API Tokens → Create API Token,权限选 **Object Read & Write**,
   范围限定到这个桶;记下 Access Key ID、Secret Access Key 和 Account ID。
2. 把 `.env.example` 拷成 `.env`,填上 R2 四项与登录密码,权限设 600:

```bash
cp .env.example .env && chmod 600 .env && vi .env
```

3. 删掉旧容器,用 compose 起(会自动读 `.env`,以后改密钥只需 `docker compose up -d`):

```bash
cd /root && docker compose up -d
```

如果你不用 compose,就自己把 `.env` 的值展开到 `docker run -e`,效果等价。

4. 拷 `scripts/offsite_backup.sh` 到 `/root/`,手工跑一次:

```bash
bash /root/offsite_backup.sh        # 应看到「校验通过 {…}」与「已上传 offsite-….sqlite3」
```

5. 上传成功后在 `crontab -e` 加一行(每天 4:00,按宿主时区):

```
0 4 * * * bash /root/offsite_backup.sh >> /var/log/sdf-backup.log 2>&1
```

cron 按**宿主**时区执行,与容器里的 `TZ` 无关。部署前先用 `timedatectl` 确认宿主时区,
上面这行就是宿主本地时间每天 4:00。

想看桶里有什么,用一次性容器自查,不需要在宿主装 rclone:

```bash
docker run --rm -e RCLONE_CONFIG_R2_TYPE=s3 -e RCLONE_CONFIG_R2_PROVIDER=Cloudflare \
  -e RCLONE_CONFIG_R2_REGION=auto -e RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true \
  -e RCLONE_CONFIG_R2_ACCESS_KEY_ID=$KEY -e RCLONE_CONFIG_R2_SECRET_ACCESS_KEY=$SECRET \
  -e RCLONE_CONFIG_R2_ENDPOINT=https://$ACCOUNT_ID.r2.cloudflarestorage.com \
  rclone/rclone:1.76.0 lsf r2:sdf-backup/sdf
```

没有配置 `SDF_R2_*` 时,`python -m app.offsite` 只打一行提示就退出(退出码 0),
不会让 cron 每天发报警。

备份逻辑在 `app/offsite.py`:用 SQLite 在线备份 API 出快照(运行中 `cp` 可能拿到半页),
`PRAGMA integrity_check` 跟各表行数校验,通过了才上传;上传成功后删 R2 里超过 `SDF_KEEP_DAYS`
(默认 30 天)的旧份,并删掉本地临时快照,免得 `/init` 页面的备份列表被刷屏。

**恢复演练必做**:备份没验证过等于没有。每季度从 R2 取一份最新的,本地覆盖跑一次
`uv run uvicorn` 看账本对不对。

## 检查

```bash
uv run python scripts/check.py     # ruff + pytest,提交前跑这个
uv run pytest -q                   # 只跑测试
```

## 使用流程

1. **装表**:在「系统 → 表管理」建表。分表要录装表读数(它自己的原点);总表不填基数 ——
   总表读数以水务、电力出具的账单为准,房东自己不抄。
2. **入住**:在「系统 → 租户」一次录完该租户住哪几块表 + 每块表当天的读数。
   同一路水电可以选多块表(两间房各一块水表就是两条计量线,各自算账)。
3. **抄表**:每次抄表当场算出这个账期的应收,没有单独的"出账"按钮。
   漏抄的读数可以按真实日期补录到中间。
4. **收款**:收款记在租户名下,不指定付的是哪一笔。系统按最早欠账优先自动冲抵,
   多收的转为预收余额,下期自动抵扣,也可以登记退款。
5. **退租**:录每条线退租当天的读数,当场结算本租期并关闭租期。
   只要求算清差异,不要求尾款已收 —— 未收部分留在台账里继续追。
6. **换表**:在「表管理」里针对某块表点"换表"。旧表终读数与新表基数同一天录入,
   **账期不切断**,用量 = 旧表段 + 新表段,合并成一张账。
7. **缴费**:照部门账单录入日期、总表、读数、单价、金额。系统不推算、不校验。
8. **统计**:按租户 / 计量线 / 账期查询用量与金额,并排显示向部门缴出、向租户收取与毛差,
   另列空档期用量。可导出 PDF。

## 关键规则

- 只有分表产生租户账单;总表只用于与部门缴费对账,不对租户计费。
- 一块分表只接一块总表;一块分表同一时间只归一个租户,先后可归多个;一个租户可用多块分表。
- "这块表现在归谁"只从租期推导,表上不存租户字段。
- 空档期(上一任走、下一任来之间)的用量归房东,算出展示但不收费。
- 读数没开始收款就能改;收过款就锁死,要改先删收款。换表与退租的读数一经录入不可改。
- 没有"删除租户"和"删除表":租户离开是退租,表退休是换表。

领域术语见 [GLOSSARY.md](GLOSSARY.md),架构决策见 [docs/adr/](docs/adr/)。

## PDF 字体

PDF 导出默认使用 `C:/Windows/Fonts/simhei.ttf`;其他平台可通过 `SDF_PDF_FONT` 环境变量指定中文字体路径。
镜像内已装 `fonts-wqy-zenhei`,找不到 Windows 字体时自动用它。
