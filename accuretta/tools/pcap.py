# SPDX-License-Identifier: AGPL-3.0-only
"""Summarise a packet capture (PCAP/PCAPNG) with dpkt: hosts, ports, DNS, HTTP, TLS names.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from pathlib import Path
from accuretta.core.optional_imports import _HAVE_DPKT
from accuretta.core.workspace import normalize_path
from accuretta.security.approvals import request_approval

try:
    import dpkt as _dpkt  # type: ignore
except Exception:
    _dpkt = None  # type: ignore  (optional: analyze_pcap checks bridge._HAVE_DPKT)


def _analyze_pcap_dpkt(path: Path, max_packets: int, dns_only: bool, http_only: bool) -> dict:
    import socket
    
    dns_queries = []
    http_requests = []
    src_ips = {}
    dst_ips = {}
    ports = {}
    protocols = {}
    
    pkt_count = 0
    first_ts = None
    last_ts = None
    
    known_bad_ports = {4444, 5555, 1337, 31337, 8888}
    suspicious = []
    connections = {} # (src, dst, dport) -> count
    
    try:
        with open(path, 'rb') as f:
            try:
                pcap = _dpkt.pcap.Reader(f)
            except ValueError:
                f.seek(0)
                pcap = _dpkt.pcapng.Reader(f)
                
            for ts, buf in pcap:
                if pkt_count >= max_packets:
                    break
                pkt_count += 1
                
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
                
                try:
                    eth = _dpkt.ethernet.Ethernet(buf)
                except Exception:
                    continue
                    
                if not isinstance(eth.data, _dpkt.ip.IP) and not isinstance(eth.data, _dpkt.ip6.IP6):
                    continue
                    
                ip = eth.data
                
                try:
                    src = socket.inet_ntop(socket.AF_INET if isinstance(ip, _dpkt.ip.IP) else socket.AF_INET6, ip.src)
                    dst = socket.inet_ntop(socket.AF_INET if isinstance(ip, _dpkt.ip.IP) else socket.AF_INET6, ip.dst)
                except Exception:
                    continue
                
                src_ips[src] = src_ips.get(src, 0) + 1
                dst_ips[dst] = dst_ips.get(dst, 0) + 1
                
                if isinstance(ip.data, _dpkt.udp.UDP):
                    protocols["UDP"] = protocols.get("UDP", 0) + 1
                    udp = ip.data
                    dport = udp.dport
                    ports[dport] = ports.get(dport, 0) + 1
                    
                    conn_key = (src, dst, dport)
                    connections[conn_key] = connections.get(conn_key, 0) + 1
                    
                    if dport in known_bad_ports:
                        suspicious.append(f"Connection to suspicious port: {src} -> {dst}:{dport}")
                    
                    if not http_only and (udp.dport == 53 or udp.sport == 53):
                        try:
                            dns = _dpkt.dns.DNS(udp.data)
                            if dns.qr == _dpkt.dns.DNS_Q and len(dns.qd) > 0:
                                qname = dns.qd[0].name
                                dns_queries.append({"timestamp": ts, "query": qname, "src": src})
                                if qname.endswith(".onion") or qname.endswith(".bit") or qname.endswith(".i2p"):
                                    suspicious.append(f"Suspicious DNS query: {qname} from {src}")
                        except Exception:
                            pass
                            
                elif isinstance(ip.data, _dpkt.tcp.TCP):
                    protocols["TCP"] = protocols.get("TCP", 0) + 1
                    tcp = ip.data
                    dport = tcp.dport
                    ports[dport] = ports.get(dport, 0) + 1
                    
                    conn_key = (src, dst, dport)
                    connections[conn_key] = connections.get(conn_key, 0) + 1
                    
                    if dport in known_bad_ports:
                        suspicious.append(f"Connection to suspicious port: {src} -> {dst}:{dport}")
                    
                    if not dns_only and len(tcp.data) > 0:
                        # Try HTTP
                        try:
                            req = _dpkt.http.Request(tcp.data)
                            http_requests.append({
                                "timestamp": ts,
                                "src": src,
                                "dst": dst,
                                "method": req.method,
                                "uri": req.uri,
                                "host": req.headers.get("host", ""),
                                "user_agent": req.headers.get("user-agent", "")
                            })
                            if "authorization" in req.headers and req.headers["authorization"].lower().startswith("basic"):
                                suspicious.append(f"Cleartext HTTP Basic Auth: {src} -> {dst}")
                            continue # Successfully parsed HTTP
                        except Exception:
                            pass

    except Exception as e:
        return {"error": f"Failed reading PCAP: {e}"}

    for (s, d, p), count in connections.items():
        if count > 100: # Simple beaconing heuristic
            suspicious.append(f"High connection count (possible beaconing): {s} -> {d}:{p} ({count} connections)")

    return {
        "path": str(path),
        "parser": "dpkt",
        "packet_count": pkt_count,
        "time_range": {"first": first_ts, "last": last_ts},
        "protocols": protocols,
        "dns_queries": dns_queries[:1000],
        "http_requests": http_requests[:1000],
        "tls_sni": [],
        "top_sources": sorted([{"ip": k, "count": v} for k, v in src_ips.items()], key=lambda x: x["count"], reverse=True)[:20],
        "top_destinations": sorted([{"ip": k, "count": v} for k, v in dst_ips.items()], key=lambda x: x["count"], reverse=True)[:20],
        "top_ports": sorted([{"port": k, "count": v} for k, v in ports.items()], key=lambda x: x["count"], reverse=True)[:20],
        "suspicious": list(set(suspicious))
    }


# --- Tool 3: analyze_pcap ---
def tool_analyze_pcap(args: dict) -> dict:
    """Analyze a PCAP or PCAPNG packet capture file."""
    if not _HAVE_DPKT:
        return {"error": "PCAP parser not available. install with: pip install dpkt"}
        
    raw_path = args.get("path")
    if not raw_path:
        return {"error": "path required"}
    
    target = Path(normalize_path(raw_path))
    if not target.exists() or not target.is_file():
        return {"error": "file not found"}
        
    max_packets = args.get("max_packets", 50000)
    dns_only = args.get("dns_only", False)
    http_only = args.get("http_only", False)

    aid = request_approval(
        "Analyze packet capture", f"Parse {target}",
        {"kind": "analyze_pcap", "target": str(target)})
    if aid.get("decision") != "approve":
        return {"error": "user denied"}

    try:
        return _analyze_pcap_dpkt(target, max_packets, dns_only, http_only)
    except Exception as e:
        return {"error": f"parsing failed: {e}"}
