"""Session-level offline policy for bridge networking."""

import ipaddress
import sys


def local_address(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    if str(host).lower() in {"localhost", "localhost."}:
        return True
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def offline_socket_guard(event, args):
    if event == "socket.getaddrinfo":
        host = args[0]
    elif event in {"socket.connect", "socket.sendto"}:
        address = args[1] if event == "socket.connect" else args[-1]
        if not isinstance(address, tuple):
            return
        host = address[0]
    else:
        return
    if host is not None and not local_address(host):
        raise OSError("Offline mode blocks external network connections. Restart with offline mode disabled to connect.")


def install_offline_guard():
    sys.addaudithook(offline_socket_guard)
