> **Historical document.** This is the original project brief that SPEC.md was
> developed from, retained for provenance. Where the two disagree, SPEC.md is
> authoritative.

I need a system for configuring and monitoring multiple internet (WAN) uplinks with a priority order, along with a daemon that monitors and provides IPv4 default route connectivity through the highest priority working uplink, as well as IPv6 connectivity for multiple working uplinks simultaneously. The IPv6 behavior should allow the client to make more intelligent decisions about which uplink to use, via source address and route selection, while preventing cross-talk (using the wrong source address for an uplink) and intelligently deprecating prefixes and routes when an uplink is no longer working.

I would like the daemon to be written in Python. The rest of the system can be in Python if they share code or it is otherwise convenient, or it can be written in bash or POSIX shell.

Drilling down into the requirements:

1. Assume ifupdown (rather than systemd-networkd) for basic interface configuration; assume dhcpcd as the DHCP and DHCPv6 client; and require the use of iproute2 and related tools for all other needs to manipulate interface configuration.

2. Assume there are multiple internal interfaces, each for a separate logical internal network. In my configuration, these are all VLAN subinterfaces of a parent trunk physical interface; but they could instead be separate physical interfaces. Allow the user to specify in configuration each logical network by interface name.

3. For IPv4:

	a. Assume that these internal interfaces come pre-configured with RFC 1918 subnets to be used by all clients for access to the highest priority working uplink via IPv4.

	b. Assume clients are either pre-configured with IPv4 addresses and routes on the correct subnet, or receive assignments via DHCP, along with valid name servers.

	c. Configure a default route for each uplink with distinct metrics corresponding to uplink priority, in preparation for its runtime behavior of removing default routes that are found no longer to be working, such that the highest-priority remaining default route will be used for all (NATed) IPv4 traffic.

4. For IPv6:

	a. Assume each internal interface comes pre-configured with a ULA address and a link local address (typically fe80::1) for internal ULA traffic routing.

	b. Assume clients all self-configure addresses, routes, RDNSS, and DNSSL via SLAAC (i.e., from router advertisements).

	c. For each uplink, configure a macvlan sub-interface on each internal interface with a distinct MAC address and link-local address, starting from fe80::2. Distinguishing which router a client intended for the next hop will be critical for clients that comply with source address selection rule 5.5 from RFC 6724, and the easiest way to accomplish this when these routers are all really the same machine is to receive the packet in a way that is distinguishable at layer 2 (i.e., on a distinct interface mediated by a distinct MAC address).

	d. For each uplink, configure an instance of dhcpcd via systemd to request prefix delegation and for each logical internal network assign a prefix to the macvlan sub-interface corresponding to that uplink.

		i. It should use the dhcpcd exit hook to configure a separate routing table for each uplink in addition to the routes dhcpcd itself adds to the main table by default.

		ii. The exit hook should also configure `ip -6 rule`s that only look up the table for the uplink associated with the inbound macvlan sub-interface (`iif`) so clients don't send packets to one virtual router and end up having those packets routed out an unexpected uplink. These tables can optionally blackhole all packets with incompatible source addresses, responding with Destination Unreachable code 6 "reject route to destination". The rules should use priorities restricted to a configurable range (default: 29000-29100) to allow for predictable behavior in combination with other policy routing use cases.

	e. Configure multiple instances of radvd via systemd, one for each uplink, with AdvDefaultPreference/AdvRoutePreference set to high for the highest priority working uplink, medium for others that are still up, and low for any that are down. Assume the administrator has preconfigured the default radvd systemd instance for advertising ULA prefixes and routes and working DNS servers.

5. The daemon should monitor each uplink at regular configurable intervals (default: every 10 seconds) and when a link appears to be down for more than 3 consecutive intervals, it should be deprovisioned.

	a. Monitoring should comprise pinging well-known hosts (e.g., google.com, a4.dscg.akamai.net) and reporting success if any respond.

	b. Deprovisioning IPv4 simply means removing the default route associated with the broken uplink.

	c. Deprovisioning IPv6 is more complicated. It requires:

		i. That advertised routes associated with that uplink be removed from all clients, or reduced in priority such that they are attempted only as a last resort.

		ii. That prefixes associated with the uplink be immediately deprecated. (The need for this goes away once all clients conform to RFC 6724's rule 5.5, as they will not attempt to use source address from one router for packets sent to another; but that process will take years.)

	d. When the daemon is stopped, it must clean up everything it configured at runtime but otherwise leave things in the state they were at startup.

It is critical that the network configuration schema be such that local traffic can reach the internet under normal conditions at boot time without a daemon running. The reason is that Debian will take five minutes to boot if the internet isn't working. So it may be that the primary interface needs to be configured as part of the default dhcpcd instance, or all the uplink instances need to be persistent systemd units.

Some additional thoughts:

I'm looking for an opinion on how to structure the functionality I need from this project. E.g., how to configure things that are semi-permanent (e.g., uplinks, macvlan configuration for internal interfaces) vs. those things that must be actively manipulated at runtime; and which things should be imperative (e.g., startup scripts, ifupdown config) vs. which should be event-driven (e.g., run as dhcpcd hooks) vs. which need to be managed actively by a daemon. If I'm intending to make this a Debian package, perhaps the internal interfaces and uplinks should be package config (e.g., `dpkg-reconfigure -plow`) that then result in files dropped into `/etc/network/interfaces.d`. But I'm open to different ways of doing this. I'd appreciate suggestions.

I would also like a comparison of what I'm asking for to what systemd-networkd provides. I have avoided it out of concern that it is immature and feature-limited, but that may no longer be true.

But first of all, let me know if there are any problems with this proposed architecture before going off to write code. For example:

1. Are there any problems with running multiple instances of dhcpcd or radvd, and if so, let me know how a single instance of each should be managed instead.

2. Will my proposal to use AdvDefaultPreference/AdvRoutePreference to guide clients to the best uplink actually work? Or do I need to advertise only the prefixes and routes from the best uplink and deprecate all others? I would really like clients compliant with rule 5.5 to be able to use the secondary uplinks for testing purposes by specifying a source address that results in selecting the corresponding next-hop router for that uplink, but I'm not even sure if that's the behavior RFC 6724 prescribes for clients compliant with rule 5.5. Coming up with a viable multi-homing pattern for IPv6 is part of the reason I'm pursuing this project.
