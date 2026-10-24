#!/usr/bin/env python3
"""proxy_doctor — AI 工具代理体检（仅标准库）

用法:
    python proxy_doctor.py                       # 自动读环境变量里的代理
    python proxy_doctor.py --proxy http://127.0.0.1:7890
    python proxy_doctor.py --proxy ... --target https://api.github.com   # 指定要测的目标

检查项:
    1. 代理环境变量（进程级 / 用户级 / 机器级 三层）
    2. 代理端口是否在监听
    3. 经代理 / 直连 的出口 IP 对比
    4. HTTP 与 HTTPS 双通道探测
    5. 结果汇总与建议
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

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


def _load_proxy_from_env() -> str | None:
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
                 "HTTP_PROXY", "http_proxy"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


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
    parser = argparse.ArgumentParser(description="AI 工具代理体检")
    parser.add_argument("--proxy", default=None, help="代理 URL，默认读环境变量")
    parser.add_argument("--target", action="append", default=[], help="额外要测的 URL")
    parser.add_argument("--skip-ip", action="store_true", help="跳过出口 IP 对比")
    args = parser.parse_args()

    proxy = args.proxy or _load_proxy_from_env()
    problems: list[str] = []
    advice: list[str] = []

    print("=" * 60)
    print("proxy_doctor — AI 工具代理体检")
    print("=" * 60)

    # 1) 环境变量三层
    print("\n[1] 代理环境变量（三层）")
    for layer, values in _env_layers().items():
        print(f"  {layer}:")
        for name, value in values.items():
            shown = _display_env_value(name, value) if name != "error" else value
            print(f"    {name:<18} = {shown}")

    if not proxy:
        problems.append("没有找到任何代理变量")
        advice.append("显式设置 HTTPS_PROXY/ALL_PROXY，或用 --proxy 指定")

    # 2) 端口
    if proxy:
        host, port = _parse_host_port(proxy)
        print(f"\n[2] 代理端口 {host}:{port}")
        if _port_open(host, port):
            print("  在监听 ✓")
        else:
            print("  未监听 ✗")
            problems.append(f"代理端口 {host}:{port} 没有监听")
            advice.append("先启动代理软件；或在 TUN 模式下检查端口是否被改写")

    # 3) 出口 IP 对比
    if proxy and not args.skip_ip:
        print("\n[3] 出口 IP 对比")
        via = _exit_ip(proxy)
        direct = _exit_ip(None)
        print(f"  经代理: {via}")
        print(f"  直连  : {direct}")
        if via == direct and via != "(取不到)":
            problems.append("经代理与直连的出口 IP 相同")
            advice.append("若开了 TUN/透明代理，两者相同是正常的（直连也被内核劫持）；"
                          "否则说明代理被规则绕过，检查该目标是否命中直连规则")
        elif via == "(取不到)":
            problems.append("经代理取不到出口 IP")
            advice.append("检查代理是否可用、节点是否在线")

    # 4) 双通道探测
    targets = list(DEFAULT_TARGETS)
    for extra in args.target:
        targets.append(("custom", extra, 200))
    print("\n[4] 目标可达性（经代理）")
    for kind, url, expect in targets:
        status, _body, err = _fetch(url, proxy)
        ok = status == expect
        mark = "✓" if ok else "✗"
        print(f"  [{kind}] {_safe_display_url(url)}")
        print(f"        -> {status} {('(期望 %s)' % expect) if status else ''} {err}".rstrip())
        if not ok:
            problems.append(f"{url} 经代理返回 {status or err}")
            advice.append(f"若 {kind.upper()} 全部失败、且 curl 在别的终端能通，先怀疑本进程/沙箱的网络限制")

    # 5) 汇总
    print("\n[5] 汇总")
    if not problems:
        print("  未发现明显问题 ✓")
    else:
        print(f"  发现 {len(problems)} 个问题：")
        for item in problems:
            print(f"    - {item}")
    if advice:
        print("\n  建议：")
        for item in dict.fromkeys(advice):  # 去重但保序
            print(f"    • {item}")

    print("\n提示：代理变量的改动只对新进程生效；已运行的应用需要重启。")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
