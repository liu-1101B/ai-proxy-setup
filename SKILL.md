---
name: ai-proxy-setup
description: "给 AI 工具（DSH、Electron 应用、Playwright 浏览器、CLI 子进程）配置本机网络代理的实战方法。覆盖三条互不替代的通道、NO_PROXY 隔离模型连接、NODE_USE_ENV_PROXY 的必要性、TUN/全局模式的副作用、按需开关与验证诊断。适用场景：AI 工具无法访问境外资源、模型调用出现 transport error、代理配了但我这边没生效、开代理后别的软件坏掉。"
license: MIT
---

# 给 AI 工具配置网络代理

> 本文来自 2026-10 在 Windows + Clash 环境里的一次真实排查与落地。**每一条判断都有实测依据**，
> 包括三个"配置看起来完全正确、实际却没生效"的坑，以及两个由代理引发的真实故障（模型连接中断、游戏无法启动）。

## 0. 先搞清一件事：代理不是一个开关，是四条通道

绝大多数"代理配了但没用"的问题，源于把下面四条通道当成一回事。**它们互不替代**：

| 通道 | 谁在用 | 认什么 | 生效时机 |
|---|---|---|---|
| **A. 环境变量** | Node / Electron 应用（DSH、VS Code、各种 CLI 工具链） | `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY`；Node 内置 fetch 还需 `NODE_USE_ENV_PROXY=1` | **进程启动时读一次**，改完必须重启进程 |
| **B. 启动参数** | Playwright 拉起的 Chrome / Edge | `--proxy-server=<url>`（+ `--proxy-bypass-list`） | 每次启动浏览器时 |
| **C. CLI 工具自身** | `curl`、`git`、`pip`、`npm` | `ALL_PROXY`（多数认）、或各自的 `--proxy` / 配置项 | 每次调用 |
| **D. 系统级** | 所有程序 | TUN 虚拟网卡 / 系统代理注册表 / PAC | 全局、持续 |

**关键结论**：DSH 这类 agent 的工具链横跨 A/B/C/D 四层——`web_fetch` 走 A，`browser_*` 走 B，argo 的 Python 子进程走 A 派生 + C，而 TUN 会同时影响全部。
**只配一层，就一定会有"某个功能没走代理"的现象。**

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

---

## 2. 隔离：`NO_PROXY` 不是可选优化，是防事故的保险

**症状**：代理配好后，AI 工具的模型对话开始报 `transport failed` / 连接中断 / 回答卡住。
**根因**：模型 API 也被塞进了代理。代理节点一抖，模型连接就断——**而模型连接是整个会话的命脉**。

**正确做法**：

```
工具出网（web_fetch / 搜索 / 下载）  →  走代理
模型与关键 API 连接                   →  直连（进 NO_PROXY）
本机与内网                            →  直连（永远）
```

实测案例：`NO_PROXY` 只排了 `127.0.0.1,localhost,::1` 时，模型 API 和遥测域名全走境外节点，**间歇性 transport error**；补上模型域名后，Clash 的连接列表里该域名的条目归零，故障消失。

**判断域名的办法**：打开代理软件的连接面板，看有哪些域名在走代理；凡是"断了会导致你无法工作"的（模型 API、认证服务、内网系统），都进 `NO_PROXY`。

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

```bash
# 需要配置文件里的 external-controller 和 secret
curl -H "Authorization: Bearer <secret>" http://127.0.0.1:9090/configs
curl -H "Authorization: Bearer <secret>" http://127.0.0.1:9090/proxies
curl -H "Authorization: Bearer <secret>" http://127.0.0.1:9090/connections
```

能拿到：当前模式（rule/global）、TUN 开关、选中节点、**每条活连接的域名与走向**。
`/connections` 的 `downloadTotal` / `uploadTotal` 是**会话累计值**，隔 20–30 秒采两次求差才是真实速率。

### 5.3 用脚本做体检

仓库内 `scripts/proxy_doctor.py`（仅标准库）：

```bash
python scripts/proxy_doctor.py --proxy http://127.0.0.1:7890
```

输出：环境变量三层状态、端口连通性、经代理/直连的出口 IP、HTTP/HTTPS 双通道测试、以及针对失败项的下一步建议。

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

**两个前提**：
1. **控制器必须先活着**（代理软件的 GUI 至少启动过一次）。完全退出时控制器不可达——**沙箱里的 agent 也无法替你拉起那个 GUI**，这一步要人来点。
2. **切 GLOBAL 是全局的**：会改变**所有**走该端口的程序，不只是 agent 的请求。用完必须还原，并把"上次的节点"记下来。

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
├── 1. 代理端口活着吗？            → 测连接（/dev/tcp 或 curl）
├── 2. 这个进程有代理环境变量吗？   → 看「进程级」值，不是注册表
├── 3. 它所在的通道对吗？          → Node→环境变量 / 浏览器→启动参数 / CLI→ALL_PROXY
├── 4. NO_PROXY 误伤了吗？         → 目标域名是否在排除列表里
├── 5. 是节点抖动吗？              → 重试 3 次，看失败是否成簇
├── 6. 是 TUN 的假 IP 吗？         → 连接是否落在 198.18.x.x
└── 7. 是沙箱子进程的假象吗？      → 换真实工具（web_fetch/浏览器）复核
```

```
模型连接报 transport error / 回答中断
└── 查 NO_PROXY 是否包含模型域名；查代理节点是否在抖
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
| `scripts/proxy_doctor.py` | 环境变量三层 + 端口 + 出口 IP + 双协议通道体检 |
| `SKILL.md`（本文） | 完整方法与判断纪律 |
| `README.md` | 快速上手版 |

## 11. 一句话总结

> **四条通道各配各的；`NO_PROXY` 保住命脉；验证要用真正使用该网络的进程；失败先重试 3 次；全局/TUN 是核武器，用前想清楚会波及谁。**
