# runbow007

李宁 TMS 订单提醒的轻量自动化程序：支持 Windows 本地运行，也支持在 Alibaba Cloud Linux 3 上以单容器部署，数据仍只使用一个 SQLite 文件。

## 已实现

- 使用 Playwright 按《在TMS系统上下载数据》的四个步骤驱动李宁 TMS：登录 → 集团订单管理 → 高级查找选预设并导出 → 下载中心取件；
- 读取 `.xls` 和 `.xlsx`，验证必要表头、订单号唯一性以及页面总数；
- 实现四类提醒：WMS过账时效、今日实际到达、合同签署异常、延迟无原因；
- SQLite 保存订单、运行批次、异常事件和飞书发送记录；
- 新异常立即提醒，R3/R4 未解决异常每天 09:00 后最多再提醒一次；
- 通过飞书自建应用发送群消息，可选真正 @ 指定用户；
- 默认演练模式，不会误发生产群；
- 支持手工下载 Excel 后走同一套校验、规则和发送链路。

## 业务规则

### R1 WMS过账时效

```text
离厂时间(承运商提货时间)为空，且当前北京时间 > WMS过账时间 + 90分钟
```

阈值在 `config.yaml` 中可调整。同一订单同一组时间只发送一次。

### R2 今日实际到达

“实际到达时间”的日期为当前北京时间日期，且状态为“运输在途（已离厂）”。消息统计订单数、箱数并列出订单明细。

### R3 合同签署异常

- 实际到达时间=签收时间、状态=已签收、合同状态=签署中：客户未完成电子签；
- 实际到达时间=签收时间、状态=运输在途（已离厂）、合同状态=已完成：运营未操作签收。

### R4 延迟无原因

“是否延迟=是”且“延迟原因”为空、空字符串或全空格。

## Windows 本地安装

PowerShell：

```powershell
git clone https://github.com/haoxumgg/runbow007.git
cd runbow007
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1
```

安装脚本会创建 `.venv`、安装项目依赖和 Playwright Chromium，并复制 `config.example.yaml` 为 `config.yaml`。

## 通用配置

1. 编辑 `config.yaml`：
   - 填写 `tms.username`；
   - 填写飞书自建应用 `app_id`；
   - 保留已确认群 ID `oc_f79000009c4f09cbdf78b55fd35ae04a`；
   - `mention_user_id` 可留空，只发送普通群消息；需要真正 @ 时再填写飞书 `open_id` 或 `user_id`；
   - 页面选择器已按真实 DOM 校准，正常不用改。
2. Windows 将密码保存到凭据管理器：

```powershell
.\.venv\Scripts\runbow007.exe --config config.yaml credentials set-tms
.\.venv\Scripts\runbow007.exe --config config.yaml credentials set-feishu
```

密码和 App Secret 不会写进仓库、YAML 或日志。

飞书应用需要开启机器人能力、发布应用、加入目标群并拥有发消息权限。发送接口和鉴权说明：

- https://open.feishu.cn/document/server-docs/im-v1/message/create
- https://open.feishu.cn/document/server-docs/api-call-guide/calling-process/get-

## 演练真实 Excel

默认不发送飞书：

```powershell
.\.venv\Scripts\runbow007.exe --config config.yaml process-file "D:\下载\hdrunbow-maintainCompanyOrderPage-1785923320669.xls" --ui-total 200
```

确认日志中的候选数量和消息模板后，显式增加 `--send` 才会真实发送。

## 自动下载

```powershell
# 每小时检查 R1/R3/R4
.\.venv\Scripts\runbow007.exe --config config.yaml run --rules R1,R3,R4

# 每天 13:30 刷新并发送 R2（正式运行时增加 --send）
.\.venv\Scripts\runbow007.exe --config config.yaml run --rules R2
```

`TmsDownloader` 严格按下面的操作流程走，CSS 选择器可以在配置中覆盖。李宁系统的导出是后台任务，程序会自动确认导出、轮询下载中心并取得成功文件。需要肉眼确认某一步时用有界面模式跑一次：

```yaml
tms:
  headless: false
```

确认后再恢复 `true`。

## TMS 操作流程

程序完全照搬人工操作，每一步都只操作**可见**的那个元素——TMS 是标签页式 SPA，打开过的页面全部留在 DOM 里，隐藏副本和真正在用的元素长得一模一样。

| 步骤 | 操作 | 定位方式 |
| --- | --- | --- |
| 一、登录 | 打开 `https://otb.lining.com/#/login`，填用户名密码，点「登录」 | `input[placeholder*='用户名']` / `input[type='password']` / `button.submit-btn` |
| 二、进入订单页 | 点左侧「订单管理」展开，再点「集团订单管理」 | 父级只在 `.el-submenu__title` 里按整词找，子级只在 `li.el-menu-item` 里按整词找（两级同名） |
| 三、筛选并导出 | 点「高级查找」→ 点预设模板选择 → 选「AI导出数据（勿动）」→ 点「查询」→ 点「导出」→ 点「确定」 | `#quickSearch` → `.page-header-title.el-popover__reference` → `.search-list .search-item` → `button.thorn6-primary-button` → `button.round-btn:has(.thorn6-icon-daoru)` → 角色按钮「确定」 |
| 四、下载中心 | 点右上角「下载中心」，找到本轮那一行，点 Excel 图标 | `li.menu-item:has(.thorn6-icon-xiazai)` → `a[href*='exportFileDowload']` |

几个必须注意的地方：

- **「高级查找」是 `#quickSearch`**，不是它旁边那个放大镜 `#searchItem`；点错会打开另一个查询框。
- **预设按整词匹配**。列表里有「正向」和「上海正向」这种互为子串的名字，而当前选中的预设名同时显示在触发器上，按文本全局找会点回触发器、把下拉收起来。
- **只点「查询」，绝不点「保存」**。预设里的日期条件（大于等于当月 1 号）由人工在 TMS 上维护，程序改了会影响所有共用这个视图的人；跨月时需要有人去 TMS 里把日期改成当月 1 号。
- **下载中心同名任务很多**（别人也在导同一个功能）。锁定本轮那一行的规则是：功能名含 `maintainCompanyOrd` + 开始时间不早于我们点「导出」的时刻 + 状态为成功，取其中最新的一条。行里的时间只精确到分钟，所以时间窗往前放宽一分钟。
- **左下角的「共 N 条」尽力读**：读到就拿去和 Excel 唯一订单数比对，读不到只记一条日志继续导出；但如果确确实实读到 0，说明表格是空的，这时候点导出 TMS 不会建任何任务，直接快速失败重试更划算。
- **表格按行序号跨表合并**。Element UI 的固定列会把一行拆进两张 `<table>`：任务名在左边那份、下载图标在右边那份。程序一次 `evaluate` 把所有可见表格按行序号合起来读，既避开这个坑，也不用几十次 locator 往返。

### 升级已有部署

`tms.selectors` 的键名跟着流程一起换了。旧的 `config.yaml` 不用改也能跑——未知键会被忽略，程序直接用上面这套新的默认值，而新默认值本来就是对的。想让配置文件和实际行为对得上，把 `config.example.yaml` 里的 `tms:` 整段抄过去即可，只需要回填 `username`。

| 旧键 | 新键 |
| --- | --- |
| `advanced_filter_button: "#searchItem"` | `advanced_search_button: "#quickSearch"` |
| `preset_name` | `preset_trigger` + `preset_item` |
| `download_button` | `export_button` |
| `date_from_input` | 已删除（日期由人工在 TMS 上维护） |
| — | 新增 `order_menu` / `order_page_menu` / `download_center_refresh` / `download_link` |

`tms.url` 建议一并改成 `https://otb.lining.com/#/login`；留着旧的 `https://otb.lining.com/#/` 也能用，SPA 未登录时会自己跳到登录页。

## 注册 Windows 定时任务

完成凭据、飞书ID和页面选择器配置并通过演练后：

```powershell
.\scripts\register-scheduled-tasks.ps1 -EnableSending
```

脚本注册：

- `Runbow007-Hourly`：全天每小时执行 R1/R3/R4；
- `Runbow007-Arrival`：每天 13:30 刷新并执行 R2。

不传 `-EnableSending` 时，定时任务只演练不发送。

## Alibaba Cloud Linux 3 部署

推荐在 Alibaba Cloud Linux 3.2104 上使用 Docker。宿主机只负责 systemd 定时器，Python、Playwright 和 Chromium 均封装在一个基于 Debian 12 的镜像中，不需要替换系统自带 Python。

### 1. 准备服务器

按照阿里云文档安装 Docker CE 和 Docker Compose 插件，并确认 Docker 已启动：

```bash
sudo systemctl enable --now docker
docker --version
docker compose version
```

服务器不需要开放入站端口。构建时需要访问 PyPI 和浏览器下载源；运行时需要通过 HTTPS 访问 `otb.lining.com` 和 `open.feishu.cn`。

### 使用 GitHub Actions 部署（推荐）

仓库包含手动工作流 `.github/workflows/deploy.yml`。它会校验固定 SSH 主机密钥、上传当前 commit、在 `/opt/runbow007` 构建镜像，并默认执行一次不会发送飞书消息的真实下载演练。它不会因 push 自动部署。

工作流会在 Alibaba Cloud Linux 3 或 Ubuntu 24.04 首次部署时，分别按阿里云或 Docker 官方软件源安装 Docker CE、Buildx 和 Compose 插件；已安装时会直接跳过。然后在仓库 `Settings → Secrets and variables → Actions` 配置：

Repository variables：

- `DEPLOY_HOST`：服务器 IP 或域名；
- `DEPLOY_USER`：拥有免密 `sudo` 的 SSH 用户，或 `root`；
- `DEPLOY_PORT`：SSH 端口，不填时使用 `22`；
- `RUNBOW007_TMS_USERNAME`：李宁 TMS 账号；
- `RUNBOW007_FEISHU_APP_ID`：飞书应用 App ID；
- `RUNBOW007_FEISHU_CHAT_ID`：接收消息的飞书群 ID。

Repository secrets：

- `DEPLOY_SSH_PRIVATE_KEY`：与服务器 `authorized_keys` 匹配的完整私钥；
- `DEPLOY_KNOWN_HOSTS`：经过人工核对的服务器 `known_hosts` 完整记录；
- `RUNBOW007_TMS_PASSWORD`：李宁 TMS 密码；
- `RUNBOW007_FEISHU_APP_SECRET`：飞书应用 App Secret。

然后打开 `Actions → deploy → Run workflow`。首次保持 `run_smoke_test=true`、`enable_timers=false`、`enable_sending=false`；候选数量验收后，才同时启用定时器和真实发送。部署冒烟下载始终强制不发送飞书，只有显式选择 `enable_sending=true` 才会修改服务器发送开关。

### 2. 部署代码

```bash
sudo git clone https://github.com/haoxumgg/runbow007.git /opt/runbow007
cd /opt/runbow007
sudo ./scripts/deploy-alinux3.sh
```

脚本会构建镜像、创建持久化目录并安装 systemd 文件，但首次不会启动定时器。以下目录由 UID `10001` 的容器用户写入：

```text
/opt/runbow007/data
/opt/runbow007/downloads
/opt/runbow007/logs
/opt/runbow007/browser-profile
```

### 3. 填写非敏感配置和密钥

编辑 `/opt/runbow007/config.yaml`，填写 TMS 账号、飞书 App ID 和群 ID。密码和 App Secret 只写到 `/etc/runbow007/secrets.env`，值建议使用单引号包裹：

```bash
sudo vi /etc/runbow007/secrets.env
sudo chmod 600 /etc/runbow007/secrets.env
```

文件格式（账号和 App ID 也可以像 GitHub Actions 一样通过环境变量提供）：

```dotenv
RUNBOW007_TMS_USERNAME='填写账号'
RUNBOW007_TMS_PASSWORD='填写实际密码'
RUNBOW007_FEISHU_APP_ID='填写 App ID'
RUNBOW007_FEISHU_APP_SECRET='填写实际密钥'
RUNBOW007_FEISHU_CHAT_ID='填写群 ID'
```

### 4. 演练并启用定时器

```bash
# 执行一次完整演练（登录、下载、校验，不发送飞书）
sudo /opt/runbow007/scripts/run-alinux3.sh hourly

# 确认演练日志
sudo journalctl -u runbow007-hourly.service -n 200 --no-pager

# 启用 7×24 定时器；此时仍默认不发送飞书
cd /opt/runbow007
sudo ./scripts/deploy-alinux3.sh --enable-timers
systemctl list-timers 'runbow007-*'
```

定时器安排：

- 每小时第 5 分钟执行 R1/R3/R4；
- 每天 13:30（Asia/Shanghai）执行 R2；
- 周末照常运行；
- 服务器重启后自动恢复，错过的任务会补跑；
- 原有文件锁继续阻止两个任务并发执行。
- 单次任务最多运行 75 分钟；锁冲突按正常跳过处理。正式发送启用后，任务失败会向同一飞书群发送告警。

正式发送前，编辑 `/etc/runbow007/runtime.env`：

```dotenv
RUNBOW007_ENABLE_SENDING=true
```

修改后下一次定时运行自动携带 `--send`。首次部署必须保持 `false`，完成候选数量核对后再开启。

常用运维命令：

```bash
systemctl status runbow007-hourly.timer runbow007-arrival.timer
sudo systemctl start runbow007-hourly.service
sudo journalctl -u runbow007-hourly.service -f
sudo systemctl disable --now runbow007-hourly.timer runbow007-arrival.timer
```

## 常用命令

```powershell
# 检查配置并初始化数据库
runbow007 --config config.yaml check-config

# 手工文件处理
runbow007 --config config.yaml process-file order.xls --rules R1,R3,R4

# 自动下载历史未完结数据（需先配置对应保存筛选）
runbow007 --config config.yaml run --dataset open_carryover --rules R1,R3,R4
```

## 数据安全

- Windows 将账号密码和飞书 App Secret 保存在凭据管理器；Linux 从权限为 `600` 的服务器密钥文件注入环境变量；
- 原始 Excel、SQLite、日志和浏览器配置目录均被 `.gitignore` 排除；
- Excel 按 `retain_days` 自动清理，默认保留 30 天；日志按天轮转并保留相同天数；
- 每次运行记录文件 SHA-256、行数、候选数、发送数和失败原因。

## 开发检查

```bash
python -m ruff check .
python -m pytest
```

