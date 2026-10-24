# ai-proxy-setup

给 AI 工具配置本机网络代理的 Windows/Linux 实战手册与诊断工具。重点不是“代理变量有没有设置”，而是确认**具体应用实际走了哪条网络通道**。

## 五条通道，互不替代

| 通道 | 例子 | 绕过配置 |
|---|---|---|
| A. 进程环境变量 | 会读取 `HTTP_PROXY` / `HTTPS_PROXY` 的客户端与子进程 | `NO_PROXY`，部分 Node 内置 fetch 需要 `NODE_USE_ENV_PROXY=1` |
| B. 浏览器启动参数 | Playwright 启动的 Chromium | 浏览器代理参数和 bypass list |
| C. CLI / 应用自有配置 | curl、git、pip、npm 或应用专属 HTTP 客户端 | 各工具自己的参数/配置 |
| D. 桌面系统代理 | Linux GNOME manual proxy；本次 DSH/Tauri + Ninja 案例 | GNOME `org.gnome.system.proxy ignore-hosts` |
| E. 内核/网络接管 | mihomo TUN、透明代理、路由、DNS | 内核规则和网络配置 |

**重要：** `NO_PROXY` 只对真实读取它的客户端生效。走 GNOME 桌面系统代理的请求要单独核对 `ignore-hosts`；走 TUN 的流量也不能只看环境变量判断。以当前连接的 mihomo `/connections` 中的 `metadata.host` 与 `chains` 为准。

## Linux / GNOME 实战检查

查看 GNOME 系统代理状态：

```bash
gsettings get org.gnome.system.proxy mode
gsettings get org.gnome.system.proxy.http host
gsettings get org.gnome.system.proxy.http port
gsettings get org.gnome.system.proxy.https host
gsettings get org.gnome.system.proxy.https port
gsettings get org.gnome.system.proxy ignore-hosts
```

查看真实目标进程携带的环境变量（仅在本机终端执行；可能暴露代理凭据，不要直接分享原始输出）：

```bash
tr '\0' '\n' < /proc/<PID>/environ | grep -Ei '^(HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY|NODE_USE_ENV_PROXY)='
```

本次实践中，DSH 专属配置在 `~/.dsh/.env`，但 DSH 桌面端的请求由 Rust/Tauri 客户端通过 GNOME 系统代理发出；`NO_PROXY` 对这条系统代理通道没有隔离作用。具体行为依客户端版本和实际网络栈而定，请在自己的机器上用当前连接验证。

## proxy_doctor.py

仅使用 Python 标准库，支持读取进程环境、DSH dotenv、Linux GNOME 系统代理、可选的 `/proc/<PID>/environ`，以及 mihomo Controller 的只读状态。

```bash
# 测试指定代理并跳过公网 IP 对比
python scripts/proxy_doctor.py --proxy http://127.0.0.1:7897 --skip-ip

# 从 mihomo Controller 读取 GLOBAL 与实时连接
export MIHOMO_CONTROLLER_SECRET='本机配置中的 secret'
python scripts/proxy_doctor.py \
  --proxy http://127.0.0.1:7897 \
  --controller http://127.0.0.1:9799 \
  --secret-env MIHOMO_CONTROLLER_SECRET \
  --pid <PID> --skip-ip

# 显示完整 NO_PROXY / GNOME ignore-hosts（可能暴露内网域名）
python scripts/proxy_doctor.py --controller http://127.0.0.1:9799 --secret-env MIHOMO_CONTROLLER_SECRET --show-bypass-hosts --skip-ip
```

`--controller` 只读请求 `/proxies` 和 `/connections`，可显示 `GLOBAL.now`、当前选择的节点/组类型，以及实时 `metadata.host` / `chains`。脚本不会自动更改 GLOBAL。远程 Controller 不允许使用明文 HTTP；本机 Loopback HTTP 可用。为避免 secret 出现在系统进程列表中，优先用 `--secret-env`，不推荐把真实 secret 直接放在命令行中。

### 故障判定纪律

1. **先检查端口，再看 GUI**：GUI 仍在不代表 mihomo 内核活着。端口/Controller 拒绝连接时，考虑内核已经退出。
2. **内核活着不代表选中节点活着**：看 `/proxies` 的 `GLOBAL.now` 及该条目的 `type`。GLOBAL 钉在叶子节点并持续恒定超时时，别只重试三次；考虑把持久配置中的默认选项改为 URLTest/Fallback 组。
3. **域名只从实时 `/connections` 复制**：不信旧日志、截图或印象；同时检查 `chains`。
4. **三层各管各的**：目标进程环境、`~/.dsh/.env`、GNOME `ignore-hosts` 不是同一份配置。
5. **Controller 改动可能不落盘**：内核重启后再次检查端口和 `GLOBAL.now`。要持久化应修改受管理的配置文件/订阅覆写，或把复查加入每次重启清单。
6. **undici 不应使用 CIDR 绕过规则**：本次使用场景中 `10.0.0.0/8` 不生效；按目标客户端支持的语法逐项配置实际 IP/hostname。

## 隐私与沙箱边界

- `proxy_doctor.py` 会对代理 URL 凭据、查询参数和自定义绕过主机名做默认脱敏；`--show-bypass-hosts` 会显式输出完整值。
- 若未指定 `--skip-ip`，脚本会通过 HTTPS 公共服务查询出口 IP；服务方会看到请求的公网 IP。
- 受限 Agent 沙箱可能无法访问 GNOME 用户会话的 D-Bus/dconf，也可能无法读取其他用户/受限 PID 的 `/proc` 环境。出现 `unavailable` 不等于代理设置为空；需要写系统代理时可能必须由用户在桌面会话中手动执行 `gsettings`。
- 不要将 Controller secret、代理账号密码或未经检查的完整诊断日志粘贴到公开 Issue。

## 相关文件

- `SKILL.md`：五通道模型、Linux/Windows 操作细节、故障树和案例。
- `scripts/proxy_doctor.py`：代理诊断脚本。
- `tests/test_proxy_doctor.py`：离线回归测试。

## License

MIT
