import ipaddress
from unittest import TestCase

from bots.tasks.run_bot_in_ephemeral_container_task import random_ipv6_address


class RandomIpv6AddressTest(TestCase):
    def test_addresses_stay_in_subnet_and_skip_network_and_gateway(self):
        subnet = ipaddress.IPv6Network("2001:db8:0:1:b0::/80")
        seen = {random_ipv6_address(str(subnet)) for _ in range(200)}
        self.assertGreater(len(seen), 190)
        for address in seen:
            ip = ipaddress.IPv6Address(address)
            self.assertIn(ip, subnet)
            self.assertNotIn(ip, {subnet.network_address, subnet.network_address + 1})

    def test_tiny_subnet_only_uses_host_addresses(self):
        seen = {random_ipv6_address("2001:db8::/126") for _ in range(100)}
        self.assertEqual(seen, {"2001:db8::2", "2001:db8::3"})
