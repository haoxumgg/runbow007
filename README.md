# runbow007

李宁 TMS 订单提醒的轻量自动化程序：一台 Windows 主机、一个 Python 程序、一个 SQLite 文件。

## 已实现

- 使用 Playwright 登录李宁 TMS、进入集团订单管理、应用保存筛选并下载全部 Excel；
- 读取 `.xls` 和 `.xlsx`，验证必要表头、订单号唯一性以及页面总数；
- 直接使用 TMS 的“预计到达时间”，不重复计算李宁系统的时效规则；
- 实现四类提醒：WMS过账时效、今日预计到达、合同签署异常、延迟无原因；
- SQLite 保存订单、运行批次、异常事件和飞书发送记录；
- 新异常立即提醒，R3/R4 未解决异常每天 09:00 后最多再提醒一次；
- 通过飞书自建应用发送群消息并真正 @ 指定用户；
- 默认演练模式，不会误发生产群；
- 支持手工下载 Excel 后走同一套校验、规则和发送链路。

## 业务规则

### R1 WMS过账时效

```text
0 <= 离厂时间 - WMS过账时间 < 90分钟
```

阈值在 `config.yaml` 中可调整。同一订单同一组时间只发送一次。

### R2 今日预计到达

直接判断 TMS 字段“预计到达时间”的日期是否为当天。运输在途订单显示订单号和箱数，已签收订单只汇总数量。

### R3 合同签署异常

- 状态=已签收，合同状态=签署中：客户未完成电子签；
- 状态=运输在途（已离厂），合同状态=已完成：运营未操作签收。

### R4 延迟无原因

“是否延迟=是”且“延迟原因”为空、空字符串或全空格。

## 安装

PowerShell：

```powershell
git clone https://github.com/haoxumgg/runbow007.git
cd runbow007
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1
```

安装脚本会创建 `.venv`、安装项目依赖和 Playwright Chromium，并复制 `config.example.yaml` 为 `config.yaml`。

## 配置

1. 编辑 `config.yaml`：
   - 填写 `tms.username`；
   - 填写飞书自建应用 `app_id`；
   - 保留已确认群 ID `oc_f79000009c4f09cbdf78b55fd35ae04a`；
   - 填写许昊的飞书 `open_id` 或 `user_id`；
   - 首次联调时校准 TMS 页面选择器。
2. 将密码保存到 Windows 凭据管理器：

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

`TmsDownloader` 优先按中文语义定位按钮，CSS 选择器可以在配置中覆盖。首次拿到账号后需要用有界面模式校准一次：

```yaml
tms:
  headless: false
```

确认稳定后再恢复 `true`。

## 注册 Windows 定时任务

完成凭据、飞书ID和页面选择器配置并通过演练后：

```powershell
.\scripts\register-scheduled-tasks.ps1 -EnableSending
```

脚本注册：

- `Runbow007-Hourly`：全天每小时执行 R1/R3/R4；
- `Runbow007-Arrival`：每天 13:30 刷新并执行 R2。

不传 `-EnableSending` 时，定时任务只演练不发送。

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

- 账号密码和飞书 App Secret 保存在 Windows 凭据管理器；
- 原始 Excel、SQLite、日志和浏览器配置目录均被 `.gitignore` 排除；
- Excel 默认保留 30 天；清理可由 Windows 任务或企业运维策略执行；
- 每次运行记录文件 SHA-256、行数、候选数、发送数和失败原因。

## 开发检查

```powershell
python -m ruff check .
python -m pytest
```
