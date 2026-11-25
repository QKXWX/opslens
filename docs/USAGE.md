# 用 opslens 给服务器做一次体检

一条命令拿到 0–100 健康评分、分级问题清单，和一份能直接发给同事的报告。只需要 Python 3.9 以上，不装任何第三方依赖。

## 1. 把文件拿到服务器上

三种方式选一种，都是同一个文件（64634 字节，sha256 `e3cd86ec77245b216187376be27349fef4c6976780b1707b5e3a5572ffeee10c`）。

**方式 A：国内直连（推荐）**

```bash
curl -fsSL https://aka.doubaocdn.com/s/FW4yueetob -o opslens.py
sha256sum opslens.py
```

**方式 B：走 GitHub 代理**

```bash
curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/QKXWX/opslens/main/opslens.py -o opslens.py
```

**方式 C：从你自己的电脑传过去**

```powershell
scp C:\path\to\opslens.py root@10.104.49.205:/root/
```

> 提示：`raw.githubusercontent.com` 在国内常被重置，报错长相是 `curl: (35) Recv failure: 连接被对方重置`。这是网络问题，不是仓库坏了，换方式 A 或 B。

**结果**：`ls -l opslens.py` 能看到文件，大小 64634 字节。

## 2. 跑第一次体检

```bash
python3 opslens.py
```

**结果**：屏幕顶部出现一行 `XX / 100 等级 X`，下面跟着五类分项和问题清单。

> 提示：默认只采样 0.6 秒 CPU，机器正忙时数据会跳。想更准就 `python3 opslens.py --sample 3`。

## 3. 看懂结果

报告分四段，从上往下读：

1. **总分**：`85 / 100 等级 B · 基本健康`。等级 A≥90、B≥80、C≥70、D≥60、F<60。
2. **五类分项**：`✔ 资源 100 / 100 · 占 30 分`。`✔` 正常、`!` 提示、`✘` 严重、`-` 跳过。
3. **优先处理**：1、2、3 按严重程度排好，直接照这个修。
4. **结尾统计**：`共 17 项检查：正常 13 · 提示 3 · 严重 1 · 跳过 0`。

扣分规则：严重项扣满该项分值，提示项扣 60%，跳过项不扣分也不得分。可执行的检查项占比低于 60% 时，顶部会写“样本不足”，不会给你一个假满分。

## 4. 生成能发人的报告

```bash
python3 opslens.py --html report.html      # 自包含单文件，双击就开，不联网也能看
python3 opslens.py --md report.md          # 贴文档、贴群里的纯文本版
python3 opslens.py --json > report.json    # 接监控、工单、自建平台
```

**结果**：`report.html` 里是深色仪表盘，含总分环、五类卡片、覆盖率、按分类分组的问题卡片。

把报告取回自己电脑看：

```powershell
scp root@10.104.49.205:/root/report.html .
start report.html
```

> 提示：`scp` 最后的 `.` 表示“存到当前目录”，所以先在 PowerShell 里 `cd` 到你想放的文件夹，不然会落在 `C:\Users\<你>`。

## 5. 只查关心的部分

```bash
python3 opslens.py --only 磁盘,安全       # 只跑这两类，分数按剩余项重新归一
python3 opslens.py --skip 服务            # 跳过某一类
```

分类名：`资源`、`磁盘`、`安全`、`服务`、`基础`，英文 key（`resource`、`disk`、`security`、`service`、`base`）也认，中英文标点都行。

## 6. 接到自动化里

**看分数够不够，用退出码判断：**

```bash
python3 opslens.py -q --fail-under 80
echo $?      # 0 达标，1 低于 80 分，2 参数写错
```

**每天 8 点自动巡检一次：**

```bash
echo '0 8 * * * /usr/local/bin/opslens -q --fail-under 80 >> /var/log/opslens.log 2>&1' | crontab -
```

**CI 里当门禁：**

```yaml
- name: 服务器基线体检
  run: python3 opslens.py --fail-under 80 --html report.html
```

## 7. 改判定阈值

默认阈值是按通用服务器定的。业务特殊就复制一份改数字：

```bash
cp rules.example.json my-rules.json
python3 opslens.py --rules my-rules.json
```

能改的键：`disk_warn` / `disk_crit`、`inode_warn` / `inode_crit`、`cpu_load_warn` / `cpu_load_crit`（按核数倍数）、`cpu_usage_warn` / `cpu_usage_crit`、`mem_warn` / `mem_crit`、`swap_warn` / `swap_crit`、`zombie_warn` / `zombie_crit`、`dns_warn_ms` / `dns_crit_ms`。写错的键会被忽略，不报错。

## 8. 装成命令

```bash
install -m 755 opslens.py /usr/local/bin/opslens
opslens --html /var/log/opslens.html
```

之后 `opslens`、`opslens --demo`、`opslens version` 都能直接用。删掉就是 `rm /usr/local/bin/opslens`。

## 9. 常见问题

**`curl: (35) Recv failure: 连接被对方重置`**
`raw.githubusercontent.com` 被网络重置。换第 1 节的方式 A 或 B。

**`opslens: 未找到命令`**
它只是个 Python 脚本，没安装入口。要么 `python3 opslens.py`，要么按第 8 节装成命令。

**磁盘只报了 `/boot`，根分区没算进去**
根分区的文件系统类型不在采集范围（常见于 `/` 是 overlay 或内存盘）。工具会把磁盘项降级成“提示”并注明“根分区 / 未纳入统计”，不再给满分。想确认根盘容量，直接用 `df -h /`。

**安全项显示“未找到 sshd_config”或整类跳过**
以非 root 跑，读不到 `/etc/ssh/sshd_config`、`/etc/shadow`。用 root 跑，或 `sudo python3 opslens.py`。

**Windows / macOS 上很多项跳过**
这是设计行为：读不到 `/proc` 就跳过，并在详情里写原因。覆盖率低于 60% 会提示“样本不足”，跨平台结果仅供参考。

**想让它一直盯着**
`python3 opslens.py --watch 60` 每 60 秒重跑一次，`Ctrl-C` 退出。长期挂后台建议用 cron 或 systemd timer，别用 `--watch`。

## 附录 A：17 项检查与分值

| 分类 | 检查项 | 分值 | 判什么 |
|---|---|---|---|
| 资源 30 | CPU 负载 | 10 | 1/5/15 分钟负载与核数比 |
| | CPU 使用率 | 6 | 采样窗口内使用率 |
| | 内存使用 | 10 | 已用占比 |
| | 交换分区 | 4 | swap 使用率，未启用则提示 |
| 磁盘 22 | 磁盘空间 | 14 | 各本地挂载点使用率取最高 |
| | inode 使用 | 8 | inode 占比，小文件堆积会先炸这里 |
| 安全 26 | SSH root 登录 | 6 | `PermitRootLogin` 是否允许 |
| | SSH 密码登录 | 6 | `PasswordAuthentication` 是否开着 |
| | 防火墙 | 6 | firewalld / ufw / nftables 是否活动 |
| | `/etc/shadow` 权限 | 4 | 是否 0000 / 0600 级别 |
| | 空密码账号 | 4 | shadow 里密码字段为空的账号数 |
| 服务 12 | systemd 失败单元 | 6 | `systemctl --state=failed` 数量 |
| | 内核更新 | 3 | 运行内核是否落后于已装最新，需重启 |
| | 时间同步 | 3 | chrony / timesyncd 是否同步 |
| 基础 10 | DNS 解析 | 4 | 解析耗时（取两次较优值） |
| | 默认路由 | 3 | 是否存在默认网关 |
| | 僵尸进程 | 3 | `Z` 状态进程数量 |

## 附录 B：全部参数

| 参数 | 作用 |
|---|---|
| `-j, --json` | 输出 JSON |
| `--html PATH` | 生成自包含 HTML 报告 |
| `--md PATH` | 生成 Markdown 报告 |
| `--only LIST` | 只查指定分类，逗号分隔 |
| `--skip LIST` | 跳过指定分类 |
| `--rules FILE` | 自定义阈值 JSON |
| `--sample SEC` | CPU 采样时长，默认 0.6 |
| `--demo` | 用内置演示数据出报告，不读本机 |
| `--watch SEC` | 每 N 秒循环巡检 |
| `--fail-under N` | 分数低于 N 时退出码 1 |
| `-q, --quiet` | 只输出一行总分 |
| `--no-color` | 关闭颜色（重定向到文件时用） |
| `version` | 打印版本 |

## 附录 C：它改不改系统

不改。全部检查都是只读：读 `/proc`、`/sys`、`sshd_config`、`systemctl` 状态、`shutil.disk_usage`，不写任何配置、不重启服务、不发外部请求（DNS 检查解析的是 `github.com`）。卸载就是删掉那一个文件。
