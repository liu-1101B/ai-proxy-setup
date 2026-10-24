---
name: ai-proxy-setup
description: "给 AI 工具配置本机网络代理的 Windows/Linux 实战手册。区分环境变量、浏览器启动参数、CLI 自有代理、GNOME 桌面系统代理与 TUN/内核接管五条通道；包含 mihomo Controller、NO_PROXY、DSH ~/.dsh/.env、gsettings 和故障树诊断。"
license: MIT
---

# 给 AI 工具配置网络代理

> 本文汇总 Windows 与 Linux 的代理排查实践。不同客户端、桌面环境、代理内核和版本可能走不同网络路径；请把“实测案例”与“通用机制”分开，最终以目标进程和实时连接数据验证。

## 0. 先搞清一件事：代理不是一个开关，是五条通道

排查时不要只看“环境变量是否正确”，而要确认**发出请求的具体进程到底走哪条通道**。五条通道可以同时存在，也互不替代：

| 通道 | 谁在用 | 认什么 | 关键边界 |
|---|---|---|---|
| **A. 进程环境变量** | 明确读取代理环境变量的运行时、库和子进程 | `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY`；部分 Node 内置 fetch 还需 `NODE_USE_ENV_PROXY=1` | **只有客户端真的读取环境变量时才生效**；不能保护转走系统代理的流量 |
| **B. 浏览器启动参数** | 例如由 Playwright 启动的 Chrome / Edge | `--proxy-server=<url>`、`--proxy-bypass-list` 或浏览器自身配置 | 只影响使用该启动参数/配置的浏览器实例 |
| **C. CLI / 应用自有设置** | `curl`、`git`、`pip`、`npm`、某些 HTTP 客户端或应用专属配置 | 各工具自身的参数、配置文件和绕过规则 | 与 A 不保证相同；同一应用内部不同组件也可能不同 |
| **D. 桌面系统代理** | 读取桌面网络设置的 GUI 应用；Linux GNOME 常通过 GSettings/dconf 暴露 | `org.gnome.system.proxy` 下的代理地址与 `ignore-hosts` | **`NO_PROXY` 对只走桌面系统代理的请求没有隔离作用**；必须检查桌面代理自己的绕过列表 |
| **E. 内核 / 网络层接管** | TUN、透明代理、路由、DNS、iptables/nftables 等 | 代理内核和系统网络配置 | 可接管原本“直连”的请求；不能只靠环境变量推断实际路由 |

**Linux 实战修正（DSH/Ninja 案例）**：这次 DSH 桌面端为 Rust/Tauri 客户端，Ninja 配置了 `system_proxy: true`，GNOME manual proxy 指向 `127.0.0.1:7897`。虽然 `deepseek.com` 已在 `NO_PROXY` 中，mihomo `/connections` 仍显示模型域名经 `GLOBAL` 出站。对这条路径，环境变量绕过规则不起作用；实际系统代理的 `ignore-hosts` 才是需要核查的层。**这是此次环境的实测，不应不加验证地推广到所有 DSH 版本、客户端和桌面环境。**

所以，“模型域名加入 `NO_PROXY`”必须附带前提：**仅当发出请求的客户端确实读取并遵循该环境变量时成立。** 应以当前 `/connections` 的 `metadata.host` 与 `chains` 为准，而不是凭 UI、进程名、旧日志或配置文件作结论。

---

## 1. 最小可用配置（Windows 用户级）

```powershell
$p = 'http://127.0.0.1:7890'   # 换成你自己的混合端口（Clash 常见 7890 / 6789 / 1080）

# 通道 A：环境变量（持久，对新进程生效）
[Environment]::SetEnvironmentVariable('HTTP_PROXY',  $p, 'User')
[Environment]::SetEnvironmentVariable('HTTPS_PROXY', $p, 'User')
[Environment]::SetEnvironmentVariable('ALL_PROXY',   $p, 'User')
[Environment]::SetEnvironmentVariable('NODE_USE_ENV_PROXY', '1', 'User')

# 关键：NO_PROXY 必须排除「本机」和「必须直连的域名」
[Environment]::SetEnvironmentVariable('NO_PROXY',
  '127.0.0.1,localhost,::1,<你的模型或内网服务域名>', 'User')
```

- **改完必须重启目标进程**（或整个应用）。环境变量是进程启动时固化的。
- 只想临时生效：`$env:HTTPS_PROXY = $p`（只影响当前窗口及其子进程，关窗口即失效）。
- 支持 `.env` 的工具（如 DSH）：写进**应用自己的 home 目录**（`~/.dsh/.env`），**不要写进项目目录**——项目目录的 `.env` 通常被视为"不可信来源"而被忽略。

### 1.1 Linux / GNOME / DSH：三份绕过配置分属不同层

本次 Linux 实战确认，至少要分开看以下三处，不能把一处配置当成另一处的替代：

1. **目标进程环境**：环境变量是进程启动时继承的快照。查看 PID（将 `<PID>` 替换为真实进程号；输出可能包含代理凭据，只在本机查看，不要直接贴到 Issue）：

   ```bash
   tr '\0' '\n' < /proc/<PID>/environ | grep -Ei '^(HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY|NODE_USE_ENV_PROXY)='
   ```

   若权限允许，诊断脚本也支持 `--pid <PID>`，仅提取代理相关键并对 URL 凭据脱敏。更可靠的方式之一是从目标应用真实启动的子进程里检查它拿到的环境，而不是在另一个终端检查自己的 shell。

2. **DSH 专属环境文件**：查看 `~/.dsh/.env`。本次实践确认该文件中的配置对对应 DSH 工具链生效；但它与 GNOME 系统代理是两份配置。诊断脚本只读取此文件中的代理相关键，不读取或显示其他变量。

   ```bash
   grep -E '^(export )?(HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY|NODE_USE_ENV_PROXY)=' ~/.dsh/.env
   ```

   这条原始命令可能显示代理 URL 中的凭据；请只在本机执行，不要直接分享完整输出。脚本会对代理 URL 凭据做脱敏。

3. **GNOME 桌面系统代理**：

   ```bash
   gsettings get org.gnome.system.proxy mode
   gsettings get org.gnome.system.proxy.http host
   gsettings get org.gnome.system.proxy.http port
   gsettings get org.gnome.system.proxy.https host
   gsettings get org.gnome.system.proxy.https port
   gsettings get org.gnome.system.proxy ignore-hosts
   ```

   GNOME 的 `ignore-hosts` 与 `NO_PROXY` 不是同一个列表。若应用使用 GNOME 桌面代理，务必在这里检查是否包含**实际连接的 hostname**。更改系统代理设置可能影响整台桌面会话；请先读取和记录原值。

**沙箱权限边界**：有些 Agent 沙箱不仅无法正确探测网络，还可能因为无法访问会话 D-Bus、`dconf` 或 `/run/user/<uid>` 而不能读取/更改 GNOME 设置。诊断输出中的 `unavailable` 不等于配置为空或代理关闭。需要改系统设置时，可能必须由用户在真实桌面会话中手动执行 `gsettings`，不要从受限沙箱里反复重试写入。

**undici 的 CIDR 限制**：按本次使用的 undici 行为，`NO_PROXY` 中写 `10.0.0.0/8` 不能作为可靠绕过规则；需要时把实际内网 IP/hostname 按该客户端支持的格式逐条列出。不同客户端的语法不完全一样，勿把一套规则假定为所有运行时都支持。

---

## 2. 隔离：`NO_PROXY` 不是可选优化，是防事故的保险

**症状**：代理配好后，AI 工具的模型对话开始报 `transport failed` / 连接中断 / 回答卡住。
**一种可能根因**：对确实读取环境变量的客户端，模型 API 未被 `NO_PROXY` 排除，导致请求经代理出站。**但这不是所有客户端的通用根因**：若应用走 GNOME 桌面系统代理、CLI 自有配置或 TUN，必须查对应通道。

**正确做法**：

```
工具出网（web_fetch / 搜索 / 下载）  →  按该工具真实使用的通道配置
模型与关键 API 连接                   →  依客户端能力选择环境变量绕过 / GNOME ignore-hosts / 其他专属规则
本机与内网                            →  按客户端与内核规则分别验证
```

历史实测案例（环境变量客户端）：`NO_PROXY` 只排了 `127.0.0.1,localhost,::1` 时，模型 API 和遥测域名全走境外节点，**间歇性 transport error**；补上模型域名后，Clash 的连接列表里该域名的条目归零，故障消失。这与本手册中的 Linux GNOME/Tauri 案例属于不同通道，不能混为一个根因。

**判断域名的硬性纪律**：

1. 先从 mihomo Controller 的 `GET /connections` 读取当前连接的 `metadata.host`，**原样复制真实 hostname**，不要从旧日志、历史截图或记忆中抄域名。本次实践曾把 `hif-dliq.deepseek.com` 写进绕过列表，但当前连接实际使用的是 `hif-leim.deepseek.com`，配置自然不会命中。
2. 同时看 `chains` 字段确认当前实际出站链路。只看到域名在列表中，不代表应用使用了该列表。
3. 再判断客户端的通道：真正读取 `NO_PROXY` 的客户端才在这里配置；走 GNOME 系统代理的应用还要检查 `org.gnome.system.proxy ignore-hosts`；走 TUN 的流量则要另外查规则和连接链。
4. 保存修改后重新触发真实请求，并再次检查实时 `/connections`；不能用“改完了配置”替代验证。

对于常见的后缀匹配实现，`.deepseek.com` 可以覆盖其子域名，但各客户端对前导点、通配符、端口、CIDR 的解释不完全一致。请按实际客户端验证，而不是假设所有实现相同。

---

## 3. `NODE_USE_ENV_PROXY=1`：为什么只设代理变量不够

**实测**：在 Node 24 下，只设 `HTTPS_PROXY` 时，对 `www.google.com` 的请求仍然 `ECONNRESET`；加上 `NODE_USE_ENV_PROXY=1` 后立刻返回 204。

原因：Node 的**内置 `fetch`** 默认不读代理环境变量（早期版本的行为），需要该开关显式启用；而基于 undici 的库（很多 Electron 应用内部用的）会自己读环境变量。

**两条路都要照顾到**：环境变量（给库）+ `NODE_USE_ENV_PROXY=1`（给内置 fetch）。这是"有些请求走代理、有些没走"最常见的原因。

---

## 4. 五个会让人误判的坑

### 坑 1：用 `pwsh` 里的 `curl.exe` 判断 HTTPS 可达性

某些环境（特定 TLS 后端 / 沙箱）里，它对**所有** HTTPS 目标返回 `000`——**连直连完全正常的站点也失败**。这是 shell 假象，不是网络结论。

→ 判 HTTPS 用 **Node fetch / Python requests / 真实浏览器**复核。HTTP 目标可以用 curl 测。

### 坑 2：用子进程的结果给代理下结论

受限 shell / 沙箱里的子进程有独立网络限制（TLS 被拦、GUI 程序无法启动、`spawn EPERM`）。它们的结果**既不能证明通，也不能证明不通**。

→ 判据必须来自**最终使用该网络的那个进程**：`web_fetch` 拿到 200 才算工具真的通；判浏览器用 `browser_open` 打开 IP 探测页，看出口。

### 坑 3：单次失败就下结论

海外节点抖动是常态。实测同一端点会**一轮 4/4 通、下一轮 0/4**。

→ **至少重试 3 次**再判断。若失败**成簇落在固定耗时点**（例如全部在 ~500ms 同时复位），先怀疑出口节点，而不是自己的配置。

### 坑 4：进程环境残留 —— "我明明删了变量，怎么还走代理"

Windows 上环境变量有三层：**机器级 / 用户级（注册表）/ 进程级（已加载在内存里）**。

- 删注册表用户级变量，**只影响新启动的进程**；已经在跑的进程（包括你的 agent 会话）仍然持有旧值。
- 这就是"注册表已经清空，但工具仍然把请求发给一个已经关闭的代理端口"的原因。

→ 清理要**三层都处理**：删注册表值 + 清当前 shell 的 `Env:` + 重启目标应用。诊断时先看**进程级**值（`$env:HTTPS_PROXY`），再看注册表。

### 坑 5：TUN 的"假 IP"

开 TUN 后，DNS 返回 `198.18.x.x` 这类保留地址，流量被内核层劫持。此时：

- 你可以用"连接落在 `198.18.0.x`"来**确认 TUN 正在接管**；
- 但**不能**用这个假 IP 去判断"代理没工作"——它本来就是中间层地址；
- 某些代理服务器会在这个状态下对特定 TLS 握手返回**连接重置**（`UND_ERR_SOCKET` / `ECONNRESET`），**写操作（push/上传）尤其容易踩**。

→ 写操作要**带指数退避重试**；或者临时关 TUN 只留端口代理。

**同类假阳性**：TUN 开着时，"经代理出口 IP"与"直连出口 IP"**相同是正常的**（直连也被劫持）。
不要据此判断代理失效——要看代理软件自己的连接面板，或临时关 TUN 再对比。

### 坑 6：诊断脚本自己先崩在编码上

中文 Windows 的控制台默认 **GBK**。脚本里打印 `✓` / `✗` / `→` 或大段中文时，
可能抛 `UnicodeEncodeError: 'gbk' codec can't encode character '\u2713'` ——
**你以为是网络问题，其实是脚本没跑完**。

```python
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
```

或直接用纯 ASCII 符号（`[OK]` / `[FAIL]`）。**写诊断脚本时，先保证它在目标机器上能完整跑完。**

---

## 5. 验证与诊断（照抄可用）

### 5.1 三步验证

```bash
# 1) 代理端口是否活着（期望 204）
curl -x http://127.0.0.1:7890 -o NUL -w "%{http_code}\n" http://www.gstatic.com/generate_204

# 2) 出口 IP 是否真的变了（对比两条通道）
curl -x http://127.0.0.1:7890 http://api.ip.sb/ip     # 走代理
curl http://api.ip.sb/ip                               # 直连

# 3) 具体工具是否真的走了代理（用工具本身，而不是 curl）
#    AI 工具：让它访问一个只有走代理才通的域名，看返回码
#    浏览器：打开 ip-api.com/json，看 country 字段
```

### 5.2 Clash / mihomo 控制器（最有用的一招）

端口取决于本机配置；本次 Linux 环境的示例是 `127.0.0.1:9799`，并非通用默认值。先检查运行态：

```bash
# 最好先将 secret 放入环境变量，避免写在命令行参数里
export MIHOMO_CONTROLLER_SECRET='替换成你的 secret'
python scripts/proxy_doctor.py \
  --proxy http://127.0.0.1:7897 \
  --controller http://127.0.0.1:9799 \
  --secret-env MIHOMO_CONTROLLER_SECRET \
  --skip-ip
```

也可以直接读取原始 API（仅在本机终端执行；不要分享含 secret 的命令/日志）：

```bash
curl -H "Authorization: Bearer $MIHOMO_CONTROLLER_SECRET" http://127.0.0.1:9799/proxies
curl -H "Authorization: Bearer $MIHOMO_CONTROLLER_SECRET" http://127.0.0.1:9799/connections
```

能拿到当前选择器和**每条活动连接的实时 hostname 与出站链路**：

- `/proxies` 中 `proxies.GLOBAL.now` 是当前 GLOBAL 选择；再在 `proxies[now].type` 里看当前选择是 `URLTest` / `Fallback` 等自动探测组，还是一个具体的叶子节点。
- 若 GLOBAL 钉在一个叶子节点且请求持续恒定超时，不要把它误判为随机节点抖动、反复重试三次。应单独确认该节点是否失效；可把 GLOBAL 切到配置中存在的 URLTest 组（例如 `♻️ 自动选择`），但本诊断脚本只读，不替你更改选择。
- `/connections` 的 `metadata.host` 是配置绕过域名时的首选事实来源，`chains` 用来核实它实际经过的代理链。域名只从当下连接复制。
- `/connections` 的 `downloadTotal` / `uploadTotal` 是**会话累计值**，隔 20–30 秒采两次求差才是真实速率。

**API 选择可能不落盘**：通过 Controller 的 `PUT /proxies/GLOBAL` 更改选择通常只是运行态；内核重启后可能回到配置文件中定义的默认节点。应把持久选择写入配置文件，或至少将“重启后复查 `GLOBAL.now`”纳入每次重启后的检查表。

### 5.3 用脚本做体检

仓库内 `scripts/proxy_doctor.py`（仅标准库）：

```bash
python scripts/proxy_doctor.py --proxy http://127.0.0.1:7890
```

输出：当前进程环境、DSH 的 `~/.dsh/.env`、Linux 目标 PID 环境（可选）、GNOME 系统代理（含 `ignore-hosts`）、代理端口状态、出口 IP 与 HTTP/HTTPS 测试。还可通过 `--controller` 只读获取 mihomo 的 `/proxies` 和 `/connections`，显示 `GLOBAL.now`、选择器类型与实时 host/chains。示例：`python scripts/proxy_doctor.py --controller http://127.0.0.1:9799 --secret-env MIHOMO_CONTROLLER_SECRET --pid <PID> --skip-ip`。默认只读，不会切换节点；若需查看完整绕过列表，可加 `--show-bypass-hosts`，但输出可能含内部域名。

---

## 6. 按需开关：不启停软件，只切出口

如果需求是"**用到时开、用完关**"，不要反复启停代理软件（重、且会波及全部流量）。更轻的做法是**复用常驻控制器的 API，只切出口**：

```js
// 关断：把 GLOBAL 指向订阅里的 REJECT 组
await fetch('http://127.0.0.1:9090/proxies/GLOBAL', {
  method: 'PUT',
  headers: { Authorization: `Bearer ${secret}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({ name: '🛑 全球拦截' }),          // 替换为你订阅里的拦截组名
});

// 开启：切回上次的节点
await fetch('http://127.0.0.1:9090/proxies/GLOBAL', {
  method: 'PUT',
  headers: { Authorization: `Bearer ${secret}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({ name: '上次用的节点名' }),
});
```

**实测效果**：关断后 Google 请求 `FAIL ECONNRESET (11ms)`（快速失败，不卡死）；恢复后 `HTTP 200 (337ms)`。切换耗时 10–300ms。

**三个前提**：
1. **控制器端口必须真的监听**。GUI 图标或进程还在，不代表 mihomo 内核活着；先测端口与 Controller，不要先看 GUI 进程列表。若端口拒绝连接但 Ninja/Clash GUI 仍在，按“GUI 存活、内核退出”的可能性排查。
2. **切 GLOBAL 是全局的**：会改变所有走该内核/端口的程序，不只是 agent 请求。用完要记录并还原原选择。
3. **运行态选择不一定持久化**：Controller API 的选择通常在内核重启后丢失。若配置文件默认仍是死节点，问题会复发。每次内核重启后要确认内核/端口已恢复，并复查 `/proxies` 的 `GLOBAL.now`；持久修复应修改受管理的配置文件/订阅覆写，而不是只做一次 API PUT。

---

## 7. 副作用清单：代理会弄坏什么

全局代理 / TUN 会影响整机流量，某些软件对此极其敏感：

| 类型 | 表现 | 处置 |
|---|---|---|
| **游戏 + 反作弊**（腾讯 ACE、EasyAntiCheat 等） | 启动报错、登录组件失败、被强制关闭 | 玩游戏时关代理 / 关 TUN；优先保留"直连"规则 |
| **证书固定的应用**（部分银行、网银客户端） | TLS 握手失败、证书错误 | 加入直连规则，或关闭 MITM |
| **内网系统 / 本地服务** | 连不上 | 必须进 `NO_PROXY` |
| **遥测/上报** | 看似无意义的持续流量 | 属正常；若要静默，把对应域名设为直连或拒绝 |
| **模型连接** | transport error、回答中断 | 进 `NO_PROXY`（见第 2 节） |

**判据**："开代理就坏、关代理就好"的软件，**优先怀疑代理**，而不是软件本身损坏。这是被真实案例验证过的一条。

---

## 8. 故障树：症状 → 排查顺序

```
工具访问境外资源失败
├── 1. 先测实际代理端口 / Controller 端口
│   ├── 端口拒绝连接，但 Ninja/Clash GUI 仍在 → 疑似内核僵尸态；GUI 活不等于内核活
│   └── 端口活、Controller 可读 → 继续
├── 2. 查 /proxies 的 GLOBAL.now 与目标类型
│   ├── GLOBAL 指向叶子节点 + 恒定超时 → 检查死节点；不要只重复重试
│   └── GLOBAL 指向 URLTest/Fallback → 再查组和节点状态
├── 3. 从 /connections 的 metadata.host 复制当前真实域名，并看 chains
├── 4. 识别客户端实际通道：环境变量 / 浏览器参数 / CLI 自有配置 / GNOME 系统代理 / TUN
├── 5. 仅对确认读取 NO_PROXY 的客户端检查该列表；GNOME 客户端另查 ignore-hosts
├── 6. 需要时分别检查进程环境、~/.dsh/.env 和 gsettings；它们互不替代
└── 7. 最后再判断节点抖动、TUN 假 IP 或沙箱网络限制
```

**区分三种“活着/可用”状态**：

- **GUI 活着 ≠ mihomo 内核活着**：端口/Controller 拒绝连接时，先查内核，而不是反复点连接。
- **内核活着 ≠ 选中的节点活着**：Controller 可达但 GLOBAL 指向叶子节点，持续固定超时，检查 `GLOBAL.now` 与节点/组健康状态。
- **节点可用 ≠ 目标应用走了那条通道**：必须用实际应用的实时 `/connections` 链路验证。

```
模型连接报 transport error / 回答中断
├── 从 /connections 取当前 metadata.host，核对 chains
├── 如果走环境变量通道：核对目标进程的 NO_PROXY / ~/.dsh/.env
└── 如果走 GNOME 系统代理：核对 org.gnome.system.proxy ignore-hosts；NO_PROXY 不会替它绕过
```

```
某个本地软件（游戏/银行）无法启动
└── 查是否开了 TUN/全局；该软件的出口 IP 是否变成了境外
```

---

## 9. 两个真实案例（都发生在这台机器上）

### 案例 A：`DeepSeek Messages transport failed`

- **表现**：配好代理后，模型对话间歇性失败。
- **误判**：先后怀疑网络、节点、软件版本。
- **真因**：`NO_PROXY` 没排模型域名 → 模型 API 与遥测都走了境外节点。
- **证据**：Clash 活连接里出现 `api.deepseek.com`、`dsh-otel-collector.*` 走 `GLOBAL>境外节点`。
- **修复**：`NO_PROXY` 补模型域名；验证该域名在连接列表里归零。
- **教训**：**工具出网与模型连接必须分开**。

### 案例 C：Linux DSH 模型请求仍被 GLOBAL 代理

- **表现**：`deepseek.com` 已放入 `NO_PROXY`，但 mihomo `/connections` 仍显示模型 hostname 经 `GLOBAL` 出站。
- **环境**：DSH 桌面端为 Rust/Tauri；Ninja 设置 `system_proxy: true`，GNOME 系统代理为 `manual`，指向 `127.0.0.1:7897`。
- **排查结论**：这条请求走的是桌面系统代理通道，不是依赖 `NO_PROXY` 的环境变量通道。应读取 GNOME 的 `ignore-hosts`，并以 `/connections` 中当前 `metadata.host` 精确核对，而不是复制旧日志中曾出现过的相似域名。
- **另外两个独立故障**：
  1. Ninja GUI 仍显示运行，但 mihomo 内核已退出；先检查端口/Controller。
  2. 内核和端口正常，但 GLOBAL 手动钉在失效节点；`GLOBAL.now` 指向具体叶子节点时，持续恒定超时不应简单归类为随机抖动。
- **重启纪律**：Controller API 修改不一定落盘；如果默认配置仍指向死节点，内核重启后会复发。每次重启先确认内核端口，再复查 `GLOBAL.now`。
- **边界**：这些结论描述本次 Linux/GNOME/DSH/Ninja 环境；其他客户端的网络栈与系统代理行为应单独验证。

### 案例 B：游戏「启动器登录组件报错」

- **表现**：开代理时启动失败（特定错误码），关代理 / 换热点后就正常。
- **先前的错误结论**：我按"TUN 关、系统代理关、hosts 未改、无相关连接"判定与代理无关。
- **用户的反证**：对照实验——**热点能进、关代理能进**。
- **真实机制**：全局/规则模式下游戏流量被改写出口；反作弊或登录组件对出口变化敏感。
- **教训两条**：
  1. **用户的对照实验优先于 agent 的静态检查**。静态检查只能排除"已发生的路径"，排除不了"路径本身被改变"。
  2. 遇到"开代理就坏"，**先假设是代理**，再去找不矛盾的解释。

---

## 10. 资产清单

| 文件 | 作用 |
|---|---|
| `scripts/proxy_doctor.py` | 环境变量 / DSH dotenv / GNOME gsettings / 可选 PID / mihomo Controller / 端口 + 出口 IP + 双协议体检 |
| `SKILL.md`（本文） | 完整方法与判断纪律 |
| `README.md` | 快速上手版 |

## 11. 一句话总结

> **四条通道各配各的；`NO_PROXY` 保住命脉；验证要用真正使用该网络的进程；失败先重试 3 次；全局/TUN 是核武器，用前想清楚会波及谁。**
