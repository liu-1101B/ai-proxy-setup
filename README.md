# ai-proxy-setup

给 AI 工具（DSH / Electron 应用 / Playwright 浏览器 / CLI 子进程）配置本机网络代理的**实战手册**。

不是"设置环境变量就完了"的教程——这里记录的是**配置看起来正确、实际却没生效**时该看哪里，
以及三个由代理引发的真实故障（模型连接中断、游戏无法启动、写操作被 TLS 重置）的复盘。

## 一句话原则

> **四条通道各配各的；`NO_PROXY` 保住命脉；验证要用真正使用该网络的进程；失败先重试 3 次；全局/TUN 是核武器。**

## 快速开始

```powershell
$p = 'http://127.0.0.1:7890'   # 换成你的混合端口

[Environment]::SetEnvironmentVariable('HTTP_PROXY',  $p, 'User')
[Environment]::SetEnvironmentVariable('HTTPS_PROXY', $p, 'User')
[Environment]::SetEnvironmentVariable('ALL_PROXY',   $p, 'User')
[Environment]::SetEnvironmentVariable('NODE_USE_ENV_PROXY', '1', 'User')   # Node 内置 fetch 需要
[Environment]::SetEnvironmentVariable('NO_PROXY',
  '127.0.0.1,localhost,::1,<模型/API/内网域名>', 'User')                    # 命脉直连
```

然后**重启目标应用**（环境变量是进程启动时读的），再验证：

```bash
python scripts/proxy_doctor.py --proxy http://127.0.0.1:7890
```

## 四条通道（互不替代）

| 通道 | 谁在用 | 认什么 |
|---|---|---|
| A. 环境变量 | Node / Electron（DSH 的 `web_fetch`、argo 子进程） | `HTTP(S)_PROXY`/`ALL_PROXY`/`NO_PROXY` + `NODE_USE_ENV_PROXY=1` |
| B. 启动参数 | Playwright 拉起的 Chrome/Edge | `--proxy-server=<url>`（不读环境变量） |
| C. CLI 自身 | `curl`/`git`/`pip`/`npm` | `ALL_PROXY` 或各自参数 |
| D. 系统级 | 一切程序 | TUN 网卡 / 系统代理 / PAC |

## 五个最容易踩的坑

1. **`NO_PROXY` 不排模型域名** → 模型连接被塞进代理 → `transport failed`（真实发生过）
2. **只设环境变量、不设 `NODE_USE_ENV_PROXY=1`** → Node 内置 fetch 照旧直连
3. **用 `pwsh` 里的 `curl.exe` 判 HTTPS** → 可能全 `000`，是 shell 假象不是网络结论
4. **用沙箱子进程的结果下结论** → 子进程有独立网络限制，既不能证通也不能证不通
5. **删了注册表变量但仍走代理** → 已运行进程持有旧值，必须重启进程

## 全局 / TUN 的副作用

会波及：**游戏与反作弊**（登录组件报错、被强制关闭）、**证书固定的客户端**、**内网系统**。
遇到"开代理就坏、关代理就好"的软件，**先假设是代理**，再去找不矛盾的解释。

## 详细内容

完整方法、故障树、按需开关、两个真实案例复盘 → [SKILL.md](SKILL.md)

## 许可

MIT
