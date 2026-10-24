#!/usr/bin/env python3
"""proxy_doctor — 本机代理诊断（仅标准库）。

默认检查当前进程环境、~/.dsh/.env（若存在）和 Linux GNOME 系统代理。
可选参数 --pid 检查目标进程的代理环境，--controller 只读读取 mihomo
/proxies 和 /connections。诊断工具不会替用户切换 GLOBAL 或写入系统代理。
"""

from __future__ import annotations

import argparse
import ast
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 中文 Windows 控制台默认 GBK，打印 ✓/✗/→ 会抛 UnicodeEncodeError。
# 显式把 stdout/stderr 切成 UTF-8，并允许无法编码时替换而非崩溃。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover - 老 Python / 重定向流
        pass

DEFAULT_TARGETS = [
    ("http", "http://www.gstatic.com/generate_204", 204),
    ("https", "https://www.gstatic.com/generate_204", 204),
    ("https", "https://api.github.com/", 200),
]
# These endpoints receive the caller's public IP when --skip-ip is not used.
# Use HTTPS so the diagnostic does not disclose it over plaintext HTTP.
IP_ENDPOINTS = ["https://api.ipify.org", "https://api.ip.sb/ip"]


PROXY_ENV_NAMES = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "NODE_USE_ENV_PROXY"
)
PROXY_ENV_LOOKUP_ORDER = (
    "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
    "HTTP_PROXY", "http_proxy"
)


def _load_proxy_from_values(values: dict[str, str]) -> str | None:
    normalized = {key.upper(): value for key, value in values.items()}
    for name in PROXY_ENV_LOOKUP_ORDER:
        value = normalized.get(name.upper(), "").strip()
        if value:
            return value
    return None


def _load_proxy_from_env() -> str | None:
    return _load_proxy_from_values(dict(os.environ))


def _parse_env_text(text: str) -> dict[str, str]:
    """Parse only proxy-related keys from a dotenv-style file; ignore all else."""
    parsed: dict[str, str] = {}
    allowed = {name.upper() for name in PROXY_ENV_NAMES}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.upper() not in allowed:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        parsed[key.upper()] = value
    return parsed


def _read_proxy_env_file(path: Path) -> tuple[dict[str, str], str]:
    try:
        if not path.is_file():
            return {}, "not found"
        return _parse_env_text(path.read_text(encoding="utf-8")), ""
    except (OSError, UnicodeError) as exc:
        return {}, type(exc).__name__


def _parse_proc_env_bytes(data: bytes) -> dict[str, str]:
    """Extract only proxy-related environment variables from /proc/PID/environ."""
    values = {}
    allowed = {name.upper() for name in PROXY_ENV_NAMES}
    for item in data.split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        key_text = key.decode("utf-8", "replace")
        if key_text.upper() in allowed:
            values[key_text.upper()] = value.decode("utf-8", "replace")
    return values


def _read_proc_env(pid: int) -> tuple[dict[str, str], str]:
    if not sys.platform.startswith("linux"):
        return {}, "available only on Linux"
    try:
        data = Path("/proc").joinpath(str(pid), "environ").read_bytes()
        return _parse_proc_env_bytes(data), ""
    except PermissionError:
        return {}, "permission denied (try the same user, or inspect from the app's own child process)"
    except FileNotFoundError:
        return {}, "PID not found or process exited"
    except OSError as exc:
        return {}, type(exc).__name__


def _safe_display_url(value: str) -> str:
    """Hide URL userinfo, query values, and fragments before logging URLs."""
    raw = value.strip()
    if not raw:
        return raw
    candidate = raw if "://" in raw else "http://" + raw
    try:
        parsed = urllib.parse.urlsplit(candidate)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        try:
            port = parsed.port
        except ValueError:
            return "<invalid URL; details hidden>"
        netloc = host + ((":" + str(port)) if port is not None else "")
        if parsed.username is not None or parsed.password is not None:
            netloc = "<redacted>@" + netloc
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = urllib.parse.urlencode([(key, "<redacted>") for key, _ in pairs])
        return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))
    except Exception:
        return "<URL hidden>"


def _safe_no_proxy(value: str) -> str:
    """Keep loopback entries visible but mask custom/internal hostnames."""
    visible = {"localhost", "127.0.0.1", "::1"}
    entries = [item.strip() for item in value.split(",") if item.strip()]
    rendered = []
    hidden_index = 0
    for entry in entries:
        if entry.lower() in visible:
            rendered.append(entry)
        else:
            hidden_index += 1
            rendered.append("<custom-entry-%d>" % hidden_index)
    return ",".join(rendered) if rendered else ""


def _display_env_value(name: str, value: str) -> str:
    if not value:
        return "(空)"
    upper = name.upper()
    if upper in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}:
        return _safe_display_url(value)
    if upper == "NO_PROXY":
        return _safe_no_proxy(value)
    return value


def _redact_text(value: str) -> str:
    """Best-effort removal of URL credentials and query values from errors."""
    value = re.sub(
        r"(?i)(https?://)[^/\s:@]+(?::[^/@\s]*)?@",
        r"\1<redacted>@",
        value,
    )
    return re.sub(
        r"([?&][^=&#\s]+)=([^&#\s]+)",
        r"\1<redacted>",
        value,
    )


def _env_layers() -> dict[str, dict[str, str]]:
    """Read process/user/machine proxy variables on Windows; process elsewhere."""
    names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "NODE_USE_ENV_PROXY")
    layers: dict[str, dict[str, str]] = {
        "process": {n: os.environ.get(n, "") for n in names},
    }
    if sys.platform == "win32":
        import winreg  # noqa: WPS433

        for layer_name, hive, subkey in (
            ("user(HKCU)", winreg.HKEY_CURRENT_USER, "Environment"),
            (
                "machine(HKLM)",
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            ),
        ):
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    values = {}
                    for name in names:
                        try:
                            values[name] = str(winreg.QueryValueEx(key, name)[0])
                        except FileNotFoundError:
                            values[name] = ""
                    layers[layer_name] = values
            except Exception as exc:  # pragma: no cover - registry permissions
                layers[layer_name] = {"error": str(exc)}
    return layers


def _parse_gsettings_array(value: str) -> list[str]:
    try:
        parsed = ast.literal_eval(value)
        if isinstance(parsed, (list, tuple)):
            return [str(item) for item in parsed]
    except (ValueError, SyntaxError):
        pass
    return []


def _run_gsettings(schema: str, key: str) -> tuple[str, str]:
    """Return (value, error) without requiring a GNOME session."""
    try:
        completed = subprocess.run(
            ["gsettings", "get", schema, key],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=2,
            check=False,
        )
    except FileNotFoundError:
        return "", "gsettings not installed"
    except subprocess.TimeoutExpired:
        return "", "timed out (desktop bus/session may be unavailable)"
    except OSError as exc:
        return "", type(exc).__name__
    if completed.returncode != 0:
        # Do not echo arbitrary stderr; it can include environment-specific details.
        return "", "unavailable (schema/key or desktop session)"
    return completed.stdout.strip(), ""


def _gnome_proxy_state() -> dict[str, str]:
    if not sys.platform.startswith("linux"):
        return {"_error": "GNOME system proxy is only checked on Linux"}
    keys = {
        "mode": ("org.gnome.system.proxy", "mode"),
        "ignore-hosts": ("org.gnome.system.proxy", "ignore-hosts"),
        "http-host": ("org.gnome.system.proxy.http", "host"),
        "http-port": ("org.gnome.system.proxy.http", "port"),
        "https-host": ("org.gnome.system.proxy.https", "host"),
        "https-port": ("org.gnome.system.proxy.https", "port"),
        "socks-host": ("org.gnome.system.proxy.socks", "host"),
        "socks-port": ("org.gnome.system.proxy.socks", "port"),
    }
    result = {}
    errors = []
    for name, (schema, key) in keys.items():
        value, error = _run_gsettings(schema, key)
        if error:
            errors.append(name + ": " + error)
        else:
            result[name] = value
    if errors:
        result["_error"] = "; ".join(errors)
    return result


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_controller_url(controller: str) -> tuple[str, str]:
    raw = controller.strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "", "use an http(s) URL with a hostname"
        if parsed.username or parsed.password:
            return "", "do not put credentials in the controller URL; use --secret or --secret-env"
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            return "", "controller URL must contain only scheme, host and optional port"
        if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
            return "", "refusing to send a controller secret over non-loopback HTTP; use localhost or HTTPS"
        # Force port parsing now so invalid ports fail before the request.
        _ = parsed.port
        return raw.rstrip("/"), ""
    except ValueError:
        return "", "invalid controller URL or port"


def _controller_get(controller: str, endpoint: str, secret: str | None) -> tuple[int, dict | None, str]:
    base, error = _validate_controller_url(controller)
    if error:
        return 0, None, error
    headers = {"Accept": "application/json", "User-Agent": "proxy-doctor/1.1"}
    if secret:
        headers["Authorization"] = "Bearer " + secret
    request = urllib.request.Request(base + endpoint, headers=headers, method="GET")
    # Never route loopback controller requests through HTTP_PROXY/ALL_PROXY.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=3) as response:
            payload = json.loads(response.read(2_000_000).decode("utf-8"))
            if not isinstance(payload, dict):
                return response.status, None, "unexpected JSON response"
            return response.status, payload, ""
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            return exc.code, None, "authentication failed; verify --secret/--secret-env"
        return exc.code, None, "HTTP error"
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (ConnectionRefusedError, TimeoutError, socket.timeout)):
            return 0, None, "connection refused or timed out"
        return 0, None, "unreachable"
    except (TimeoutError, socket.timeout):
        return 0, None, "request timed out"
    except (ValueError, json.JSONDecodeError):
        return 0, None, "invalid JSON response"
    except OSError:
        return 0, None, "unreachable"


def _proxy_map(payload: dict) -> dict:
    proxies = payload.get("proxies", payload)
    return proxies if isinstance(proxies, dict) else {}


def _summarize_global(proxies_payload: dict) -> tuple[str, str, str, bool]:
    """Return (current selection, selected type, message, selection-is-leaf)."""
    proxies = _proxy_map(proxies_payload)
    global_proxy = proxies.get("GLOBAL", {})
    current = str(global_proxy.get("now", "")).strip()
    selected = proxies.get(current, {})
    selected_type = str(selected.get("type", "")).strip()
    group_types = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"}
    is_leaf = bool(current and selected_type and selected_type not in group_types)
    if not current:
        message = "GLOBAL 当前选择未在响应中给出；请在 mihomo 面板/API 中核对。"
    elif is_leaf:
        message = (
            "GLOBAL 当前指向叶子节点，而不是 URLTest/Fallback 等自动探测组。"
            "若请求持续恒定超时，检查该节点是否失效；可考虑将 GLOBAL 切到自动选择组。"
            "诊断脚本不会自动切换节点。"
        )
    else:
        message = "未发现 GLOBAL 直接钉在叶子节点的明显迹象；这不证明所选节点可用。"
    return current or "(unknown)", selected_type or "(unknown)", message, is_leaf


def _connection_rows(payload: dict) -> list[tuple[str, str, str]]:
    rows = []
    for connection in payload.get("connections", []) or []:
        if not isinstance(connection, dict):
            continue
        metadata = connection.get("metadata") or {}
        host = str(metadata.get("host") or metadata.get("destinationIP") or "(unknown)")
        chains = connection.get("chains") or []
        chain = " > ".join(str(item) for item in chains) if chains else "(chain unknown)"
        rule = str(connection.get("rule") or "")
        rows.append((host, chain, rule))
    return sorted(set(rows), key=lambda row: (row[0].lower(), row[1], row[2]))


def _matching_proxy_processes() -> list[tuple[int, str]]:
    """List probable proxy GUI/core processes by comm only; never print cmdline."""
    if not sys.platform.startswith("linux") or not Path("/proc").is_dir():
        return []
    markers = ("mihomo", "clash", "ninja", "flclash")
    matches = []
    try:
        entries = Path("/proc").iterdir()
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
            if any(marker in comm.lower() for marker in markers):
                matches.append((int(entry.name), comm[:80]))
        except OSError:
            continue
    return sorted(matches)


def _display_env_layer(layer_name: str, values: dict[str, str], show_bypass_hosts: bool) -> None:
    print("  " + layer_name + ":")
    for name, value in values.items():
        if name == "error":
            print("    access: " + value)
            continue
        if not value:
            shown = "(空)"
        elif name.upper() in {"NO_PROXY", "no_proxy"} and show_bypass_hosts:
            shown = value
        else:
            shown = _display_env_value(name, value)
        print(f"    {name:<18} = {shown}")


def _print_gnome_proxy(show_bypass_hosts: bool) -> None:
    print("\n[GNOME 桌面系统代理（gsettings）]")
    state = _gnome_proxy_state()
    if "_error" in state and not any(key in state for key in ("mode", "http-host", "https-host", "socks-host")):
        print("  unavailable: " + state["_error"])
        print("  这通常表示没有可用的 GNOME user session / dconf bus，或当前沙箱禁止访问。")
        return
    print("  mode = " + state.get("mode", "(unavailable)"))
    for scheme in ("http", "https", "socks"):
        host = state.get(scheme + "-host", "(unavailable)")
        port = state.get(scheme + "-port", "(unavailable)")
        print(f"  {scheme} = {host}:{port}")
    ignored = _parse_gsettings_array(state.get("ignore-hosts", ""))
    if show_bypass_hosts:
        rendered = repr(ignored) if ignored else state.get("ignore-hosts", "(empty/unavailable)")
    else:
        rendered = _safe_no_proxy(",".join(ignored)) if ignored else "(empty/unavailable)"
    print("  ignore-hosts = " + rendered)
    if "_error" in state:
        print("  partial read: " + state["_error"])
    print("  注：ignore-hosts 是桌面代理自己的绕过列表，不等于 NO_PROXY。")
    print("  内部主机名默认脱敏；需本机核对细项时加 --show-bypass-hosts。")


def _print_controller_diagnostics(controller: str, secret: str | None) -> None:
    print("\n[mihomo Controller（只读）]")
    proxies_status, proxies_payload, proxies_error = _controller_get(controller, "/proxies", secret)
    connections_status, connections_payload, connections_error = _controller_get(controller, "/connections", secret)
    if proxies_payload is None:
        print(f"  GET /proxies: FAIL ({proxies_status or 'no response'}; {proxies_error})")
    else:
        current, selected_type, message, is_leaf = _summarize_global(proxies_payload)
        print(f"  GET /proxies: OK ({proxies_status})")
        print(f"  GLOBAL.now = {current}")
        print(f"  selected proxy type = {selected_type}")
        print("  " + message)
    if connections_payload is None:
        print(f"  GET /connections: FAIL ({connections_status or 'no response'}; {connections_error})")
    else:
        rows = _connection_rows(connections_payload)
        print(f"  GET /connections: OK ({connections_status}); active connections = {len(rows)}")
        if not rows:
            print("  未发现活动连接；只能说明采样时刻没有返回连接，不能证明目标从未走过代理。")
        for host, chain, rule in rows[:100]:
            print(f"  host={host} | chains={chain} | rule={rule or '(none)'}")
        if len(rows) > 100:
            print(f"  ... 其余 {len(rows) - 100} 条已省略")
        print("  纪律：需要配置绕过规则的域名，只从当前 /connections 的 metadata.host 复制；不要沿用旧日志/记忆。")


def _print_linux_process_env(pid: int, show_bypass_hosts: bool) -> None:
    print(f"\n[目标进程环境（/proc/{pid}/environ）]")
    values, error = _read_proc_env(pid)
    if error:
        print("  unavailable: " + error)
        return
    _display_env_layer(f"pid {pid}", values, show_bypass_hosts)


def _parse_host_port(proxy_url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(proxy_url if "://" in proxy_url else "http://" + proxy_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _fetch(url: str, proxy: str | None, timeout: float = 15.0) -> tuple[int, bytes, str]:
    """返回 (status, body, err)。proxy=None 表示直连。"""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))  # 显式清空，避免继承环境变量
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "proxy-doctor/1.0"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(4096), ""
    except urllib.error.HTTPError as exc:  # 有响应但非 2xx
        return exc.code, b"", f"HTTPError {exc.code}"
    except Exception as exc:  # noqa: BLE001 - 诊断脚本，需要吞掉所有网络异常
        return 0, b"", f"{type(exc).__name__}: {_redact_text(str(exc))[:160]}"


def _exit_ip(proxy: str | None) -> str:
    for url in IP_ENDPOINTS:
        status, body, err = _fetch(url, proxy)
        if status == 200 and body:
            text = body.decode("utf-8", "replace").strip()
            try:  # ip-api.com/line 返回 "ip,country"
                payload = json.loads(text)
                if isinstance(payload, dict) and "query" in payload:
                    return f"{payload['query']} ({payload.get('country', '?')})"
            except json.JSONDecodeError:
                pass
            return text.splitlines()[0][:80]
        if err:
            continue
    return "(取不到)"


def main() -> int:
    parser = argparse.ArgumentParser(description="AI 工具代理体检（Linux / Windows）")
    parser.add_argument("--proxy", default=None, help="要测试的代理 URL，默认取进程环境或 ~/.dsh/.env")
    parser.add_argument("--target", action="append", default=[], help="额外要测的 URL")
    parser.add_argument("--skip-ip", action="store_true", help="跳过出口 IP 对比")
    parser.add_argument("--pid", type=int, default=None, help="Linux 下读取目标 PID 的 /proc/PID/environ")
    parser.add_argument("--controller", default=None, help="mihomo Controller 基础 URL，例如 http://127.0.0.1:9799")
    parser.add_argument("--secret", default=None, help="Controller Bearer secret（注意可能出现在进程列表；优先用 --secret-env）")
    parser.add_argument("--secret-env", default="MIHOMO_CONTROLLER_SECRET", help="从指定环境变量读取 Controller secret（默认 MIHOMO_CONTROLLER_SECRET）")
    parser.add_argument("--show-bypass-hosts", action="store_true", help="显示精确 NO_PROXY / GNOME ignore-hosts 条目；输出可能含内部域名")
    args = parser.parse_args()

    dsh_env_path = Path.home() / ".dsh" / ".env"
    dsh_values, dsh_env_error = _read_proxy_env_file(dsh_env_path)
    proxy = args.proxy or _load_proxy_from_env()
    if not proxy:
        proxy = _load_proxy_from_values(dsh_values)
        if proxy:
            print("未找到进程级代理变量；诊断代理来自 ~/.dsh/.env（只用于本次测试，不修改进程环境）。")

    problems: list[str] = []
    advice: list[str] = []

    print("=" * 60)
    print("proxy_doctor — AI 工具代理体检")
    print("=" * 60)

    # 1) 当前进程环境变量（Windows 同时读取用户/机器级注册表）。
    print("\n[1] 进程 / 用户 / 机器代理环境")
    for layer, values in _env_layers().items():
        _display_env_layer(layer, values, args.show_bypass_hosts)

    # 2) DSH 专属 dotenv 文件。只读代理相关键，不读取或打印其他 secret。
    print(f"\n[2] DSH 专属环境（{dsh_env_path}）")
    if dsh_env_error == "not found":
        print("  文件不存在")
    elif dsh_env_error:
        print("  文件不可读: " + dsh_env_error)
    elif dsh_values:
        _display_env_layer("~/.dsh/.env", dsh_values, args.show_bypass_hosts)
    else:
        print("  文件存在，但未找到代理相关变量")

    # 3) 可选读取某个目标应用真正启动时携带的环境。
    if args.pid is not None:
        if args.pid <= 0:
            parser.error("--pid must be a positive integer")
        _print_linux_process_env(args.pid, args.show_bypass_hosts)

    # 4) GNOME 桌面系统代理是独立通道，与 NO_PROXY 不互通。
    if sys.platform.startswith("linux"):
        _print_gnome_proxy(args.show_bypass_hosts)

    if not proxy:
        problems.append("未找到可用于主动连通性测试的代理 URL")
        advice.append("用 --proxy 指定代理；或确认进程环境 / ~/.dsh/.env 中存在代理变量")

    # 5) 监听端口。GUI 进程存在不代表 mihomo 内核/监听端口还活着。
    if proxy:
        try:
            host, port = _parse_host_port(proxy)
        except ValueError:
            host, port = "", 0
        print(f"\n[代理监听端口] {host}:{port}")
        if host and port and _port_open(host, port):
            print("  LISTENING")
        else:
            print("  NOT LISTENING")
            problems.append(f"代理端口 {host}:{port} 未监听")
            matches = _matching_proxy_processes()
            if matches:
                names = ", ".join(f"{pid}:{name}" for pid, name in matches[:12])
                print("  检测到疑似代理 GUI/内核进程: " + names)
                print("  可能是 GUI 仍在但 mihomo 内核已退出的“内核僵尸态”；请以端口/Controller 是否可达为准。")
                advice.append("先验证实际代理端口和 Controller 端口；不要仅凭 Ninja/Clash 图标或 GUI 进程判断内核存活")
            else:
                advice.append("先启动/重启代理内核并确认监听端口；单看 GUI 进程不构成内核存活证据")

    # 6) mihomo Controller：只读 /proxies 与 /connections，不会自动切节点。
    if args.controller:
        secret = args.secret or os.environ.get(args.secret_env, "")
        try:
            controller_host, controller_port = _parse_host_port(args.controller)
        except ValueError:
            controller_host, controller_port = "", 0
        if controller_host and controller_port and not _port_open(controller_host, controller_port):
            print(f"\n[Controller 端口] {controller_host}:{controller_port} NOT LISTENING")
            matches = _matching_proxy_processes()
            if matches:
                names = ", ".join(f"{pid}:{name}" for pid, name in matches[:12])
                print("  疑似 GUI/代理进程仍存在: " + names)
                print("  GUI 活着但 Controller 端口不通，需按“GUI 与内核分离”方向排查。")
            else:
                print("  未发现常见代理进程名；仍应以端口和 Controller 响应判断内核状态。")
        _print_controller_diagnostics(args.controller, secret)
        if args.secret:
            print("  安全提示：--secret 可能短暂暴露在系统进程列表；推荐用环境变量并传 --secret-env。")

    # 7) 出口 IP 对比。使用 HTTPS 公共端点；可用 --skip-ip 跳过。
    if proxy and not args.skip_ip:
        print("\n[出口 IP 对比]")
        via = _exit_ip(proxy)
        direct = _exit_ip(None)
        print(f"  经代理: {via}")
        print(f"  直连  : {direct}")
        if via == direct and via != "(取不到)":
            advice.append("两条路径出口 IP 相同不一定代表故障；TUN/透明代理也可能接管直连流量。")
        elif via == "(取不到)":
            advice.append("经代理取不到出口 IP；检查代理端口、GLOBAL 当前选择和节点健康状态。")

    # 8) HTTP/HTTPS 目标探测。多次恒定超时与随机抖动必须分开判断。
    targets = list(DEFAULT_TARGETS)
    for extra in args.target:
        targets.append(("custom", extra, 200))
    test_path = "指定代理 " + _safe_display_url(proxy) if proxy else "直连（未指定 --proxy）"
    print("\n[目标可达性（" + test_path + "；不是对所有应用通道的证明）]")
    for kind, url, expect in targets:
        status, _body, err = _fetch(url, proxy)
        ok = status == expect
        print(f"  [{kind}] {_safe_display_url(url)}")
        print(f"        -> {status} {('(期望 %s)' % expect) if status else ''} {err}".rstrip())
        if not ok:
            problems.append(f"{_safe_display_url(url)} 经指定代理返回 {status or err}")
            advice.append("若失败是恒定超时，先检查 GLOBAL.now 与该节点健康；不要把所有失败都归因于节点抖动。")

    print("\n[结论与建议]")
    if not problems and not advice:
        print("  未发现明显问题；这不证明每条应用网络通道都正确。")
    else:
        if problems:
            print(f"  检测到 {len(problems)} 项待核查问题：")
            for item in problems:
                print("    - " + item)
        if advice:
            print("  建议：")
            for item in dict.fromkeys(advice):
                print("    - " + item)

    print("\n诊断纪律：")
    print("  1. 模型流量到底走哪条通道，应由实际应用 /connections 中的当前 host 与 chains 判断。")
    print("  2. 配置绕过域名只从实时 /connections metadata.host 复制，不信旧日志或印象。")
    print("  3. 环境变量 NO_PROXY 只对实际读取它的客户端生效；GNOME system proxy 使用自己的 ignore-hosts。")
    print("  4. GLOBAL 的 API 选择可能只存在于运行态；重启内核后必须复查 GLOBAL.now。")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
